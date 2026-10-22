"""Clean up the mic: `pugrec audio SESSION` -> SESSION/mic-clean.flac (derived).

The recording keeps the raw PodMic (lossless); this makes it sound good:
  1. DeepFilterNet 3 (neural speech enhancement): background noise and keyboard
     taps out. -D compensates its STFT/lookahead delay, so sync is kept.
  2. Breaths: Silero VAD finds where speech is; everything else is lowered by
     BREATH_DB with padding and smooth fades. Decided over whole segments with
     lookahead (offline), so it can't chatter like a live level gate did on main-pc
     (breath sits ~10 dB above the floor: see the dotfiles' podmic.conf).
  3. Tone: high-pass, a touch less low-mid mud, de-esser, compressor, then
     two-pass loudness normalisation to -16 LUFS / -1.5 dBTP. No 2-5 kHz boost
     (brings up mouth clicks - podmic.conf again).
  4. Breaths inside speech: the VAD counts a breath between phrases as speech, so
     someone who talks without long pauses keeps every breath at full level (a
     5-minute demo came out 314 s "speech" of 322 s, 4 segments). Found here from
     the sound itself - unpitched, quieter than the voice, a breathy (not hissy)
     spectrum, long enough not to be an "s" - and lowered by BREATH_IN_SPEECH_DB.
Same length and start as the recorded track, so it drops in for it.

Tools are fetched once into ~/.cache/pugrec and pinned by SHA-256.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from . import jsonl

# The mic track can start a little after the video's PTS 0 (macOS: ScreenCaptureKit's mic
# starts ~0.15 s after the first frame). Extracting it to a plain file drops that offset,
# so pad the start with silence: every extract then starts at session t=0, like the file.
# Both platforms: on Linux the start is already 0 (nothing to pad), but async=1 also fills a
# gap of more than 0.1 s mid-stream with silence, so a dropped stretch can't shift what follows.
PAD_TO_ZERO = "aresample=async=1:first_pts=0"

DF_VERSION = "0.5.6"
# Per platform: the release asset and its SHA-256 (pinned on first download; GitHub lists none).
DF_BUILDS = {
    "linux": ("x86_64-unknown-linux-musl", "70775e251eee44c0f2451a1e833326cf8bcbbe304d3e7cd12851e6fce72ef7da"),
    "darwin": ("aarch64-apple-darwin", "4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081"),
}
_DF_TARGET, DF_SHA256 = DF_BUILDS["darwin" if sys.platform == "darwin" else "linux"]
DF_URL = (f"https://github.com/Rikorose/DeepFilterNet/releases/download/v{DF_VERSION}/"
          f"deep-filter-{DF_VERSION}-{_DF_TARGET}")
DF_NAME = f"bin/deep-filter-{DF_VERSION}" + ("-macos" if sys.platform == "darwin" else "")
VAD_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
VAD_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"

SR = 48000
BREATH_DB = -18.0           # how far non-speech is lowered (not muted: muting sounds unnatural)
VAD_ON, VAD_OFF = 0.5, 0.35  # hysteresis on speech probability
PAD_PRE, PAD_POST = 0.20, 0.30   # s kept around speech (word onsets, tails)
MERGE_GAP = 0.30            # s: shorter pauses stay at full level
FADE = 0.04                 # s raised-cosine ramps
# Breaths inside speech (step 4). Measured on the normalised output, so the levels are
# absolute: speech's voiced frames sit around -14 dBFS there, breaths around -42..-25.
BREATH_IN_SPEECH_DB = -16.0  # lowered, not muted, like BREATH_DB
BREATH_SR = 16000
BREATH_FRAME, BREATH_HOP = 640, 320   # 40 ms frames every 20 ms
BREATH_VOICED_MAX = 0.45    # normalised autocorrelation peak (pitch 50-400 Hz): above = voiced
BREATH_RMS = (-52.0, -20.0)  # dBFS: below = silence (nothing to lower), above = too loud for a breath
BREATH_CENTROID = (1100.0, 4000.0)  # Hz: "s"/"sh"/"f" sit above ~4 kHz, voiced murmurs below ~1 kHz
BREATH_MIN = 0.22           # s: shorter unvoiced runs are consonants
BREATH_FADE = 0.03          # s: ramps, inside the run so word edges stay at full level
TONE = ("highpass=f=80,"
        "equalizer=f=300:t=q:w=1:g=-2,"
        "deesser=i=0.5,"
        "acompressor=threshold=-24dB:ratio=3:attack=5:release=120:makeup=1")
# After the gain to ~target: shave the few peaks that would stop linear loudnorm short of
# -16 LUFS (it won't exceed the true-peak ceiling, so it would settle lower). ~-2.5 dBFS.
LIMIT = "alimiter=limit=0.75:attack=2:release=50:level=false"
TARGET_LUFS = -16.0
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _cache():
    d = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pugrec")
    os.makedirs(d, exist_ok=True)
    return d


def fetch(url, sha256, name, executable=False):
    p = os.path.join(_cache(), name)
    if not os.path.exists(p):
        print(f"audio: downloading {url}", file=sys.stderr)
        os.makedirs(os.path.dirname(p), exist_ok=True)  # bin/ only exists once something made it
        urllib.request.urlretrieve(url, p + ".part")
        os.replace(p + ".part", p)
    with open(p, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != sha256:
            raise SystemExit(f"audio: {p} doesn't match the pinned SHA-256; delete it to re-download")
    if executable:
        os.chmod(p, 0o755)
    return p


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def read_f32(path, sr):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


# ------------------------------------------------------------------ speech --
def speech_probs(x16):
    """Silero VAD v5 over 16 kHz mono float: one probability per 512 samples (32 ms)."""
    import numpy as np
    import onnxruntime as ort
    sess = ort.InferenceSession(fetch(VAD_URL, VAD_SHA256, "silero_vad.onnx"), providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros(64, np.float32)  # v5 wants the previous 64 samples in front of each chunk
    sr = np.array(16000, np.int64)
    probs = []
    for i in range(0, len(x16) - 511, 512):
        chunk = x16[i:i + 512]
        out, state = sess.run(None, {"input": np.concatenate([ctx, chunk])[None, :], "state": state, "sr": sr})
        ctx = chunk[-64:]
        probs.append(float(out[0, 0]))
    return probs


def speech_segments(probs, hop=512 / 16000):
    """[(start_s, end_s)] of speech: hysteresis, padding, short gaps merged."""
    segs, on, start = [], False, 0.0
    for i, p in enumerate(probs):
        t = i * hop
        if not on and p >= VAD_ON:
            on, start = True, t
        elif on and p < VAD_OFF:
            on = False
            segs.append((start, t + hop))
    if on:
        segs.append((start, len(probs) * hop))
    padded = [(max(0.0, a - PAD_PRE), b + PAD_POST) for a, b in segs]
    merged = []
    for a, b in padded:
        if merged and a - merged[-1][1] < MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def gain_curve(n, segs, sr=SR, floor_db=BREATH_DB, fade=FADE):
    """Per-sample gain: 1 in speech, floor outside, raised-cosine fades at the edges."""
    import numpy as np
    floor = 10 ** (floor_db / 20)
    g = np.full(n, floor, np.float32)
    f = max(1, int(fade * sr))
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, f))).astype(np.float32)  # 0 -> 1
    for a, b in segs:
        i0, i1 = int(a * sr), min(n, int(b * sr))
        if i1 <= i0:
            continue
        g[i0:i1] = 1.0
        lo = max(0, i0 - f)  # fade in before the segment, fade out after it
        g[lo:i0] = np.maximum(g[lo:i0], floor + (1 - floor) * ramp[f - (i0 - lo):])
        hi = min(n, i1 + f)
        g[i1:hi] = np.maximum(g[i1:hi], floor + (1 - floor) * ramp[::-1][:hi - i1])
    return g


# ---------------------------------------------------------- breaths in speech --
def breath_features(x, sr=BREATH_SR, frame=BREATH_FRAME, hop=BREATH_HOP):
    """Per frame: level (dBFS), voicing (autocorrelation peak at a pitch lag) and the
    spectral centroid (Hz). Mono float; batched FFTs so a 5-minute take takes seconds."""
    import numpy as np
    x = np.asarray(x, np.float32)
    nf = max(0, (len(x) - frame) // hop + 1)
    rms, voi, cen = (np.zeros(nf, np.float32) for _ in range(3))
    lo, hi = int(sr / 400), int(sr / 50)            # pitch lags: 400 Hz .. 50 Hz
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    win = np.hanning(frame).astype(np.float32)
    for c0 in range(0, nf, 2048):
        idx = np.arange(c0, min(nf, c0 + 2048))[:, None] * hop + np.arange(frame)[None, :]
        f = x[idx]
        f = f - f.mean(axis=1, keepdims=True)
        e = (f * f).sum(axis=1)
        rms[c0:c0 + len(f)] = 10 * np.log10(e / frame + 1e-12)
        ac = np.fft.irfft(np.abs(np.fft.rfft(f, 2 * frame, axis=1)) ** 2, axis=1)[:, :frame]
        voi[c0:c0 + len(f)] = np.where(e > 1e-9, ac[:, lo:hi].max(axis=1) / np.maximum(ac[:, 0], 1e-12), 0)
        p = np.abs(np.fft.rfft(f * win, axis=1)) ** 2
        cen[c0:c0 + len(f)] = (p * freqs).sum(axis=1) / (p.sum(axis=1) + 1e-12)
    return rms, voi, cen


def breath_runs(rms, voi, cen, hop=BREATH_HOP / BREATH_SR):
    """[(start_s, end_s)] of breaths: unvoiced, breath-level, breath-coloured frames for at
    least BREATH_MIN (a one-frame dropout inside a run doesn't split it)."""
    import numpy as np
    c = ((voi < BREATH_VOICED_MAX) & (rms > BREATH_RMS[0]) & (rms < BREATH_RMS[1])
         & (cen > BREATH_CENTROID[0]) & (cen < BREATH_CENTROID[1]))
    filled = c.copy()
    filled[1:-1] |= c[:-2] & c[2:]
    runs, i, n = [], 0, len(filled)
    while i < n:
        if filled[i]:
            j = i
            while j < n and filled[j]:
                j += 1
            if (j - i) * hop >= BREATH_MIN:
                runs.append((i * hop, j * hop))
            i = j
        else:
            i += 1
    return runs


def dip_curve(n, runs, sr=SR, floor_db=BREATH_IN_SPEECH_DB, fade=BREATH_FADE):
    """Per-sample gain: floor inside each run, 1 elsewhere, raised-cosine ramps inside the run's
    edges (so the sound next to it - the end or start of a word - is never touched)."""
    import numpy as np
    floor = 10 ** (floor_db / 20)
    g = np.ones(n, np.float32)
    f = max(1, int(fade * sr))
    down = (floor + (1 - floor) * (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, f)))).astype(np.float32)  # 1 -> floor
    for a, b in runs:
        i0, i1 = int(a * sr), min(n, int(b * sr))
        if i1 - i0 < 2 * f:
            continue
        g[i0:i0 + f] = np.minimum(g[i0:i0 + f], down)
        g[i0 + f:i1 - f] = np.minimum(g[i0 + f:i1 - f], floor)
        g[i1 - f:i1] = np.minimum(g[i1 - f:i1], down[::-1])
    return g


# ------------------------------------------------------------------- clean --
def clean(session_dir, out_name="mic-clean.flac"):
    import numpy as np
    recs = jsonl.read(os.path.join(session_dir, "session.jsonl"))
    screen = next(r for r in recs if r["type"] == "stream_start" and r["stream"] == "screen")
    if not screen.get("audio"):
        raise SystemExit("audio: this session has no mic track")
    src = os.path.join(session_dir, screen["file"])
    import shutil
    # A deep-filter on PATH (e.g. shipped in the PugPal image) wins; else our pinned download.
    df = shutil.which("deep-filter") or fetch(DF_URL, DF_SHA256, DF_NAME, executable=True)
    with tempfile.TemporaryDirectory(prefix="pugrec-audio-") as tmp:
        raw_wav = os.path.join(tmp, "mic.wav")
        # Channel 0 only (wf-recorder stores the mono mic as identical stereo).
        _ff("-i", src, "-map", "0:a:0", "-af", PAD_TO_ZERO + ",pan=mono|c0=c0", "-ar", str(SR), "-c:a", "pcm_f32le", raw_wav)
        print("audio: 1/4 DeepFilterNet (noise, keyboard)", file=sys.stderr)
        subprocess.run([df, "-D", "-o", os.path.join(tmp, "df"), raw_wav], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        den = read_f32(os.path.join(tmp, "df", "mic.wav"), SR)
        raw = read_f32(raw_wav, SR)
        n = len(raw)
        den = np.pad(den, (0, max(0, n - len(den))))[:n]  # exactly the original length

        print("audio: 2/4 speech detection (breaths)", file=sys.stderr)
        segs = speech_segments(speech_probs(read_f32(os.path.join(tmp, "df", "mic.wav"), 16000)))
        gated = den * gain_curve(n, segs)
        gated_wav = os.path.join(tmp, "gated.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                        "-c:a", "pcm_f32le", gated_wav], input=gated.astype(np.float32).tobytes(), check=True)

        print("audio: 3/4 tone + loudness", file=sys.stderr)
        # Pass 1: loudness after the tone chain -> the gain to the target. Pass 2: that gain +
        # limiter, measured for loudnorm. Pass 3: loudnorm applied linearly (no pumping).
        def measure(chain):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-i", gated_wav, "-af", f"{chain},{LOUDNORM}:print_format=json",
                                "-f", "null", "-"], capture_output=True, text=True, check=True)
            return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr).group(0))
        gain = TARGET_LUFS - float(measure(TONE)["input_i"])
        chain = f"{TONE},volume={gain:.2f}dB,{LIMIT}"
        m = measure(chain)
        ln = (f"{LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
              f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        toned_wav = os.path.join(tmp, "toned.wav")
        _ff("-i", gated_wav, "-af", f"{chain},{ln},aresample={SR}", "-c:a", "pcm_f32le", toned_wav)

        # After loudness normalisation, so BREATH_RMS means the same thing on every take.
        print("audio: 4/4 breaths inside speech", file=sys.stderr)
        toned = read_f32(toned_wav, SR)
        breaths = breath_runs(*breath_features(read_f32(toned_wav, BREATH_SR)))
        out = os.path.join(session_dir, out_name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                        "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", out + ".part.flac"],
                       input=(toned * dip_curve(len(toned), breaths)).astype(np.float32).tobytes(), check=True)
        os.replace(out + ".part.flac", out)
    speech_s = sum(b - a for a, b in segs)
    breath_s = sum(b - a for a, b in breaths)
    print(f"audio: {len(segs)} speech segments, {speech_s:.1f}s of {n / SR:.1f}s; "
          f"{len(breaths)} breaths inside speech, {breath_s:.1f}s -> {out}", file=sys.stderr)
    return out, segs
