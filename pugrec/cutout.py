"""Camera cut-out: the speaker matted out of camera.mkv (Robust Video Matting), the background restyled.

Derived files, made on demand (`pugrec matte EDIT`, or by build/preview) into <session>/cutout/:
  matte-<hash>.mkv   the alpha (luma of H.264, full range) at 30 fps on the session clock, over the
                     span of the session the edit's cut-out scenes use
  <style>-<hash>.mkv the camera with its background restyled (frost, blur), same frames
Delete either to redo it. The resolver names them (plan["derive"]); nothing here decides framing.

The style maths is ported from pugcam (the PugPal dotfiles' live camera, pugcam.py: Pipeline.composite,
frost, grain) - numpy/OpenCV instead of torch, offline frames instead of a live loop. pugcam is not
used at run time. RVM (GPL-3.0, github.com/PeterL1n/RobustVideoMatting) is downloaded on first use with
a pinned SHA-256, never vendored, and run on the CPU with onnxruntime (no GPU memory used: the desktop
shares the card).
"""

import hashlib
import math
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np

MODEL_URL = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx"
MODEL_SHA256 = "88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828"
FPS = 30            # derived streams: the camera's nominal rate, resampled on the session clock
PREROLL = 45        # frames run through RVM before a chunk (its recurrent state settles; not kept)
ALGO = 1            # bump when the style maths changes: the file names change, old files are redone
STYLES = ("frost", "blur")
DEFAULTS = {"strength": 0.2, "feather": 6}   # 0.2 = pugcam's default 20: noticeable, not loud


def key(*parts):
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:10]


# ------------------------------------------------------------------ style --
def _gauss(x, sigma):
    """pugcam's gaussian_blur: radius int(3 sigma), edges replicated."""
    r = max(1, int(sigma * 3))
    return cv2.GaussianBlur(x, (2 * r + 1, 2 * r + 1), sigma, borderType=cv2.BORDER_REPLICATE)


def _down(x, size):
    return cv2.resize(x, size, interpolation=cv2.INTER_AREA)   # = torch bilinear with antialias


_grain = {}


def grain(h, w):
    """The frosted glass's texture: fine, soft and still (fixed seed), as pugcam's grain(); signed, in
    8-bit levels (int16)."""
    g = _grain.get((h, w))
    if g is None:
        n = np.random.default_rng(7).standard_normal((h, w)).astype(np.float32)
        g = _grain[(h, w)] = np.round(_gauss(n, 0.8) * 0.035 * 255).astype(np.int16)[..., None]
    return g


def composite(src8, pha, style="frost", strength=DEFAULTS["strength"], feather=DEFAULTS["feather"]):
    """src8 (H, W, 3) uint8 RGB, pha (H, W) float32 0..1 -> uint8 frame with its background restyled.
    strength 0..1 = pugcam's 0..100; feather = px (at 1080p) the outline fades over.

    pugcam's maths, reordered for memory bandwidth (several of these run at once and full-size float
    passes were the bottleneck): everything blurry - the normalised blur, frost's glow and colour, and
    the feather's fill of "you, blurred" - is done at the working size and upsampled once; only the
    edge (the feathered alpha) and the final blend are full size, the blend in uint8.

    style "none": the camera as it is - the colour a `behind:` scene draws over its source with the
    matte as alpha (on the matte's own frame grid, so the two line up frame for frame)."""
    if style == "none":
        return src8
    H, W = pha.shape
    u = H / 1080
    sigma = (2 + strength * 100 * 0.4) * u
    k = max(1, min(4, int(sigma / 3)))
    sigma /= k
    size = (W // k, H // k)
    small = (_down(src8, size) if k > 1 else src8).astype(np.float32) * (1 / 255)
    pha_s = _down(pha, size) if k > 1 else pha
    bg_w = 1 - pha_s
    # Normalised blur: average only background pixels (no halo of you smeared into the room).
    b = _gauss(np.concatenate([small * bg_w[..., None], bg_w[..., None], small], 2), sigma)
    num, den, plain = b[..., :3], b[..., 3:4], b[..., 4:]
    bg = num / np.maximum(den, 1e-3)
    trust = np.clip(den / 0.05, 0, 1)
    bg = bg * trust + plain * (1 - trust)
    if style == "frost":
        glow = _gauss(cv2.resize(bg, (bg.shape[1] // 4, bg.shape[0] // 4), interpolation=cv2.INTER_AREA),
                      max(2.0, bg.shape[1] / 160))
        glow = cv2.resize(glow, (bg.shape[1], bg.shape[0]), interpolation=cv2.INTER_LINEAR)
        bg = bg * 0.6 + glow * 0.4
        grey = bg.mean(2, keepdims=True)
        bg = (grey + (bg - grey) * 0.75) * 0.9 + 0.06
    p = pha
    if feather > 0:
        # The outline goes out of focus inside itself, into a blurred copy of you (pugcam's feather):
        # under you the background is "you, blurred" (here mixed at the working size).
        f = max(1, int(round(feather * u)))
        inner = cv2.erode(pha, np.ones((2 * f + 1, 2 * f + 1), np.uint8), borderType=cv2.BORDER_REPLICATE)
        p = np.minimum(_gauss(inner, f / 2), pha)
        bg = bg * bg_w[..., None] + plain * pha_s[..., None]
    bg8 = cv2.resize(np.clip(bg * 255 + 0.5, 0, 255).astype(np.uint8), (W, H), interpolation=cv2.INTER_CUBIC) \
        if k > 1 else np.clip(bg * 255 + 0.5, 0, 255).astype(np.uint8)
    if style == "frost":
        bg8 = np.clip(bg8 + grain(H, W), 0, 255).astype(np.uint8)
    return cv2.blendLinear(src8, bg8, p, 1 - p)


# ------------------------------------------------------------------ matting --
class Matter:
    """RVM on one run of consecutive frames (recurrent: feed them in order)."""

    def __init__(self, threads=4):
        import onnxruntime as ort
        from .audio import fetch
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(fetch(MODEL_URL, MODEL_SHA256, "rvm_mobilenetv3_fp32.onnx"), so,
                                         providers=["CPUExecutionProvider"])
        self.rec = [np.zeros((1, 1, 1, 1), np.float32)] * 4

    def __call__(self, rgb8):
        """rgb8 (H, W, 3) uint8 -> alpha (H, W) float32."""
        h = rgb8.shape[0]
        ratio = np.array([min(1.0, 270 / h)], np.float32)   # RVM's coarse pass at ~270 lines, as at 1080p
        x = rgb8.transpose(2, 0, 1)[None].astype(np.float32) * (1 / 255)
        _, pha, *self.rec = self.sess.run(None, {"src": x, "r1i": self.rec[0], "r2i": self.rec[1],
                                                 "r3i": self.rec[2], "r4i": self.rec[3], "downsample_ratio": ratio})
        return pha[0, 0]


# ------------------------------------------------------------------ files --
def _decoder(file, offset, start, n, w, h, pix):
    """n frames of a stream at FPS on the session clock from `start` (as compose.Decoder)."""
    seek = max(0.0, start - offset - 1.0)
    vf = f"setpts=PTS+{offset:.6f}/TB,fps={FPS}:start_time={start:.6f}"
    return subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{seek:.3f}", "-copyts", "-i", file, "-vf", vf,
                             "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", pix, "-"], stdout=subprocess.PIPE)


def _encoder(path, w, h, pix, crf):
    """Raw frames -> H.264 (VAAPI-decodable, for pugrec-gpu), full range, tagged BT.601 like the camera."""
    return subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}",
                             "-r", str(FPS), "-i", "-", "-vf", "scale=out_range=full:out_color_matrix=bt601",
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-g", str(FPS), "-threads", "2",
                             "-pix_fmt", "yuv420p", "-color_range", "pc", "-colorspace", "bt470bg",
                             "-color_primaries", "bt470bg", "-color_trc", "smpte170m", "-f", "matroska", path],
                            stdin=subprocess.PIPE)


def _read(proc, size):
    buf = bytearray()
    while len(buf) < size:
        b = proc.stdout.read(size - len(buf))
        if not b:
            return None
        buf += b
    return bytes(buf)


def _chunk(job):
    """Worker: frames [i0, i1) of one derived stream (matte and/or styled part files)."""
    r, i0, i1, parts = job
    cv2.setNumThreads(1)   # several workers at once: each one single-threaded outside RVM, or they fight
    w, h = r["size"]
    t0 = r["from"] + i0 / FPS
    n = i1 - i0
    need_matte = not os.path.exists(r["matte"])
    pre = min(PREROLL, i0 + int(r["from"] * FPS)) if need_matte else 0   # RVM warms up on frames before
    cam = _decoder(r["camera"], r["offset"], t0 - pre / FPS, n + pre, w, h, "rgb24")
    mat_in = None if need_matte else _decoder(r["matte"], r["from"], t0, n, w, h, "gray")
    mat_out = _encoder(parts["matte"], w, h, "gray", 6) if need_matte else None
    out = _encoder(parts["styled"], w, h, "rgb24", 14) if r.get("style") else None
    matter = Matter() if need_matte else None
    last = None
    try:
        for k in range(n + pre):
            raw = _read(cam, w * h * 3)
            if raw is None:
                if last is None:
                    raise RuntimeError(f"no camera frames at {t0:.2f}s")
                raw = last          # the camera ended early: hold its last frame
            last = raw
            src8 = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            if matter is not None:
                pha = matter(src8)
                if k < pre:
                    continue
                mat_out.stdin.write((pha * 255 + 0.5).clip(0, 255).astype(np.uint8).tobytes())
            else:
                m = _read(mat_in, w * h)
                pha = np.frombuffer(m, np.uint8).reshape(h, w).astype(np.float32) / 255 if m else pha
            if out is not None:
                out.stdin.write(composite(src8, pha, r["style"], r["strength"], r["feather"]).tobytes())
    finally:
        for p in (mat_out, out):
            if p:
                p.stdin.close()
                p.wait()
        for p in (cam, mat_in):
            if p:
                try:
                    p.stdout.close()
                    p.wait(timeout=5)   # it stops by itself after -frames:v (a kill makes it complain)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    return n


def _concat(parts, dest):
    lst = dest + ".txt"
    with open(lst, "w") as f:
        for path, n in parts:
            f.write(f"file '{os.path.abspath(path)}'\nduration {n / FPS:.6f}\n")   # Matroska: explicit durations
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-f", "matroska", dest + ".part"], check=True)
    os.replace(dest + ".part", dest)
    os.remove(lst)


def derive(r, workers=None, progress=True):
    """Make one derived stream (plan["derive"] entry) and its matte, if missing. Parallel chunks:
    RVM barely speeds up with threads (4 -> 8: 103 -> 93 ms a 1080p frame), processes scale."""
    if os.path.exists(r["file"]) and (not r.get("style") or os.path.exists(r["matte"])):
        return
    os.makedirs(os.path.dirname(r["file"]), exist_ok=True)
    n = int(round((r["to"] - r["from"]) * FPS))
    # Memory bandwidth is the limit (a Ryzen 3900X tops out at ~9 frames/s whether 3x4, 5x4 or 6x2
    # processes x RVM threads): 3 workers get all of it and leave the desktop some room.
    if workers is None:
        workers = max(1, min(3, (os.cpu_count() or 4) // 4))
    size = max(FPS * 5, math.ceil(n / max(1, workers)))
    tmp = r["file"] + ".parts"
    os.makedirs(tmp, exist_ok=True)
    jobs = []
    for i0 in range(0, n, size):
        i1 = min(n, i0 + size)
        jobs.append((r, i0, i1, {"matte": os.path.join(tmp, f"m{i0:07d}.mkv"),
                                 "styled": os.path.join(tmp, f"s{i0:07d}.mkv")}))
    need_matte = not os.path.exists(r["matte"])
    what = ("matting + " if need_matte else "") + (r.get("style") or "")
    done = 0
    pool = ProcessPoolExecutor(min(workers, len(jobs))) if workers else None   # 0 = in this process
    try:
        for got in (pool.map(_chunk, jobs) if pool else map(_chunk, jobs)):
            done += got
            if progress:
                print(f"\rcutout: {what} {done}/{n} frames", end="", file=sys.stderr, flush=True)
    finally:
        if pool:
            pool.shutdown()
    if progress:
        print(file=sys.stderr)
    if need_matte:
        _concat([(j[3]["matte"], j[2] - j[1]) for j in jobs], r["matte"])
    if r.get("style"):
        _concat([(j[3]["styled"], j[2] - j[1]) for j in jobs], r["file"])
    for j in jobs:
        for p in j[3].values():
            if os.path.exists(p):
                os.remove(p)
    os.rmdir(tmp)


def ensure(plan, **kw):
    """Every derived stream a plan names, made if missing."""
    for r in (plan.get("derive") or {}).values():
        derive(r, **kw)
