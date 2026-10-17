"""Speech to text: `pugrec transcript SESSION` -> SESSION/transcript.jsonl (derived).

whisper.cpp (whisper-cli) with the Vulkan backend: GPU transcription on AMD (RADV),
built by scripts/build-whisper.sh into ~/.cache/pugrec/bin. macOS: Homebrew's whisper-cpp
(`brew install whisper-cpp`), Metal on Apple Silicon. CPU fallback (-ng) if the GPU path fails. Input: mic-clean.flac if present (pugrec audio), else the raw
mic track - both start at the session's time 0.

Lines (session clock, ns, like every pugrec file):
  {"type": "meta", "t": 0, "model": .., "backend": "vulkan"|"metal"|"cpu", "seconds": .., "rtf": ..}
  {"type": "segment", "t": .., "end": .., "text": "..", "words": [{"t", "end", "w", "p"}]}
Word times come from whisper's DTW token alignment (-dtw), falling back to the
token offsets.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

from . import jsonl
from .audio import PAD_TO_ZERO

WHISPER_VERSION = "v1.9.4"
MODEL = "ggml-large-v3-turbo-q8_0.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL}"
MODEL_SHA256 = "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1"
DTW_PRESET = "large.v3.turbo"
FILLERS = {"um", "uh", "uhm", "umm", "erm", "er", "eh", "ehm", "hmm", "mm"}


def _cache():
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pugrec")


def whisper_cli():
    """Our own build (scripts/build-whisper.sh) if there is one, else a whisper-cli on PATH
    (e.g. shipped in the PugPal image)."""
    import shutil
    p = os.path.join(_cache(), "bin", f"whisper-cli-{WHISPER_VERSION}")
    if os.access(p, os.X_OK):
        return p
    found = shutil.which("whisper-cli")
    if found:
        return found
    how = "brew install whisper-cpp" if sys.platform == "darwin" else "run scripts/build-whisper.sh"
    raise SystemExit(f"transcript: no whisper-cli ({p} or on PATH) - {how}")


def model_path():
    p = os.path.join(_cache(), MODEL)
    if not os.path.exists(p):
        raise SystemExit(f"transcript: model missing - download {MODEL_URL} to {p}")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    if h.hexdigest() != MODEL_SHA256:
        raise SystemExit(f"transcript: {p} doesn't match the pinned SHA-256; delete it and re-download")
    return p


def audio_input(session_dir):
    clean = os.path.join(session_dir, "mic-clean.flac")
    if os.path.exists(clean):
        return clean, "mic-clean.flac"
    recs = jsonl.read(os.path.join(session_dir, "session.jsonl"))
    screen = next(r for r in recs if r["type"] == "stream_start" and r["stream"] == "screen")
    if not screen.get("audio"):
        raise SystemExit("transcript: this session has no mic track")
    return os.path.join(session_dir, screen["file"]), screen["file"]


def words_from_tokens(tokens, seg_end_ms):
    """whisper.cpp tokens -> words. A token starting with a space starts a new word;
    special tokens ([_BEG_], [_TT_..]) are dropped. Times in ms."""
    words = []
    for tok in tokens:
        text = tok.get("text", "")
        if not text or text.startswith("[_"):
            continue
        dtw = tok.get("t_dtw", -1)
        start = dtw * 10 if dtw is not None and dtw >= 0 else tok["offsets"]["from"]
        if text.startswith(" ") or not words:
            words.append({"t": start, "end": tok["offsets"]["to"], "w": text.strip(), "p": [tok.get("p", 1.0)]})
        else:  # continuation of the previous word ("demo" + "nstrate")
            words[-1]["w"] += text
            words[-1]["end"] = tok["offsets"]["to"]
            words[-1]["p"].append(tok.get("p", 1.0))
    for i, w in enumerate(words):  # a word ends where the next starts (DTW gives starts best)
        nxt = words[i + 1]["t"] if i + 1 < len(words) else seg_end_ms
        w["end"] = max(w["t"], min(w["end"], nxt)) if nxt >= w["t"] else w["end"]
        w["p"] = round(min(w["p"]), 3)
    return [w for w in words if w["w"]]


def vocabulary(prompt=None, theme=None):
    """Names whisper should spell right, from: config.toml `vocabulary`, a theme's
    `vocabulary`, and --prompt (free text, added as is). Deduplicated, in that order."""
    words = []
    try:
        from .recorder import config
        words += list(config().get("vocabulary") or [])
    except (OSError, ValueError):
        pass
    if theme:
        from .edit import script
        words += script.theme_vocabulary(theme)
    if prompt:
        words += [w.strip() for w in prompt.split(",") if w.strip()]
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def transcribe(session_dir, language="auto", prompt=None, theme=None):
    src, src_name = audio_input(session_dir)
    cli, model = whisper_cli(), model_path()
    vocab = vocabulary(prompt, theme)
    with tempfile.TemporaryDirectory(prefix="pugrec-transcript-") as tmp:
        wav = os.path.join(tmp, "in.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-af", PAD_TO_ZERO,
                        "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", wav], check=True)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    wav], capture_output=True, text=True).stdout.strip() or 0)
        base = [cli, "-m", model, "-f", wav, "-l", language, "-ojf", "-of", os.path.join(tmp, "out"),
                "-dtw", DTW_PRESET, "-np"]
        if vocab:  # whisper's initial prompt biases spelling; carried into every 30 s window
            base += ["--prompt", ", ".join(vocab) + ".", "--carry-initial-prompt"]
        backend = "metal" if sys.platform == "darwin" else "vulkan"
        t0 = time.monotonic()
        r = subprocess.run(base, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"transcript: GPU run failed ({r.stderr.strip().splitlines()[-1:]}); retrying on the CPU",
                  file=sys.stderr)
            backend = "cpu"
            t0 = time.monotonic()
            r = subprocess.run(base + ["-ng"], capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"transcript: whisper-cli failed: {r.stderr.strip()[-500:]}")
        elapsed = time.monotonic() - t0
        with open(os.path.join(tmp, "out.json"), encoding="utf-8") as f:
            data = json.load(f)

    ms = 1_000_000  # ms -> ns
    out_path = os.path.join(session_dir, "transcript.jsonl")
    n_words = 0
    with open(out_path + ".part", "w", encoding="utf-8") as out:
        out.write(json.dumps({"type": "meta", "t": 0, "model": MODEL, "whisper": WHISPER_VERSION, "backend": backend,
                              "source": src_name, "language": data.get("result", {}).get("language"),
                              "seconds": round(elapsed, 2), "rtf": round(elapsed / dur, 3) if dur else None,
                              "vocabulary": vocab}) + "\n")
        for seg in data.get("transcription", []):
            words = words_from_tokens(seg.get("tokens", []), seg["offsets"]["to"])
            if not words:
                continue
            n_words += len(words)
            out.write(json.dumps({"type": "segment", "t": seg["offsets"]["from"] * ms, "end": seg["offsets"]["to"] * ms,
                                  "text": seg["text"].strip(),
                                  "words": [{"t": w["t"] * ms, "end": w["end"] * ms, "w": w["w"], "p": w["p"]}
                                            for w in words]}, ensure_ascii=False) + "\n")
    os.replace(out_path + ".part", out_path)
    speed = dur / elapsed if elapsed else 0
    print(f"transcript: {n_words} words, {dur:.1f}s audio in {elapsed:.1f}s on {backend} ({speed:.0f}x realtime)"
          f" -> {out_path}", file=sys.stderr)
    return out_path


def load(session_dir):
    """[segment records], or None if there's no transcript."""
    p = os.path.join(session_dir, "transcript.jsonl")
    if not os.path.exists(p):
        return None
    return [r for r in jsonl.read(p) if r.get("type") == "segment"]


def suggest_cuts(segments, min_pause=1.2, keep=0.3, fillers=True):
    """Cut suggestions in source seconds: pauses longer than min_pause (keeping `keep`
    on each side, so speech doesn't sound clipped; None = no pause cuts) and filler words."""
    words = [w for s in segments for w in s["words"]]
    cuts = []
    for a, b in zip(words, words[1:]) if min_pause else ():
        gap = (b["t"] - a["end"]) / 1e9
        if gap > min_pause:
            cuts.append([round(a["end"] / 1e9 + keep, 3), round(b["t"] / 1e9 - keep, 3)])
    for w in words if fillers else ():
        if w["w"].lower().strip(".,!?…-") in FILLERS:
            cuts.append([round(w["t"] / 1e9 - 0.05, 3), round(w["end"] / 1e9 + 0.05, 3)])
    return sorted(cuts)
