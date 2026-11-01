"""The GPU build compositor's Python half: the plan's layers snapped to pixels exactly as
compose.py snaps them, streamed as JSON lines to pugrec-gpu (native/pugrec-gpu), whose NV12
frames go straight into the encoder. compose.py stays the reference and the CPU fallback.

Only geometry rounding happens here (the same int()/round() calls as compose.py, so the
banker's rounding agrees); pugrec-gpu only draws. Text and images become RGBA sprites made by
compose.text_image / compose.image_bgra, so fonts, kerning and Lanczos scaling are identical.
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading

import cv2
import numpy as np

from . import compose

GPU_VERSION = "1"   # bump with scripts/build-gpu.sh's VERSION
RENDER_NODE = "/dev/dri/renderD128"
VRAM_LIMIT_MB = 2048   # over the baseline at start; pugrec-gpu stops (exit 99) above it


def binary():
    """Our build (scripts/build-gpu.sh), or None."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = os.path.join(cache, "pugrec", "bin", f"pugrec-gpu-{GPU_VERSION}")
    return p if os.access(p, os.X_OK) and os.path.exists(RENDER_NODE) else None


def _rgb(color):
    return [int(c) for c in compose.hex_bgr(color)[::-1]]


def crop_params(fw, fh, rect, ow, oh):
    """compose.crop_scaled's integer crop, resize size and sub-pixel shift:
    [x0, y0, x1, y1, cw, ch, shift_x, shift_y, area]."""
    x0f, y0f, vw, vh = rect
    sx, sy = vw / ow, vh / oh
    x0, y0 = max(0, int(math.floor(x0f)) - 1), max(0, int(math.floor(y0f)) - 1)
    x1, y1 = min(fw, int(math.ceil(x0f + vw)) + 2), min(fh, int(math.ceil(y0f + vh)) + 2)
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    cw, ch = max(1, round((x1 - x0) / sx)), max(1, round((y1 - y0) / sy))
    kx, ky = cw / (x1 - x0), ch / (y1 - y0)
    return [x0, y0, x1, y1, cw, ch, (x0f - x0) * kx, (y0f - y0) * ky, 1 if sx >= 1 else 0]


class Job:
    """Turns plan frames into pugrec-gpu's JSON lines (sprites written to `tmp` as needed)."""

    def __init__(self, plan, tmp):
        self.plan = plan
        self.tmp = tmp
        self.W, self.H = plan["output"]["size"]
        self.ids = {s: i for i, s in enumerate(plan["streams"])}
        self.sprites = {}

    def header(self):
        streams = [{"file": s["file"], "offset": s.get("offset", 0.0)} for s in self.plan["streams"].values()]
        return {"job": {"w": self.W, "h": self.H, "fps": self.plan["output"]["fps"], "streams": streams,
                        "vram_mb": VRAM_LIMIT_MB}}

    def _sprite(self, key, make):
        """A raw RGBA file for (bgr, alpha) from make(), once per key."""
        hit = self.sprites.get(key)
        if hit is None:
            bgr, alpha = make()
            h, w = bgr.shape[:2]
            rgba = np.empty((h, w, 4), np.uint8)
            rgba[..., :3] = bgr[..., ::-1]
            rgba[..., 3] = np.clip(np.rint(alpha[..., 0] * 255), 0, 255).astype(np.uint8)
            path = os.path.join(self.tmp, hashlib.sha1(repr(key).encode()).hexdigest() + ".rgba")
            with open(path, "wb") as f:
                f.write(rgba.tobytes())
            hit = self.sprites[key] = (path, w, h)
        return hit

    def _mask(self, w, h, shape, radius):
        """An R8 file of compose.mask(w, h, shape, radius) - made by compose itself, so the
        anti-aliased edges are exactly the CPU build's."""
        key = ("mask", w, h, shape, radius)
        hit = self.sprites.get(key)
        if hit is None:
            m = compose.mask(w, h, shape, radius)
            path = os.path.join(self.tmp, hashlib.sha1(repr(key).encode()).hexdigest() + ".r8")
            with open(path, "wb") as f:
                f.write(np.clip(np.rint(m[..., 0] * 255), 0, 255).astype(np.uint8).tobytes())
            hit = self.sprites[key] = path
        return hit

    def _coverage(self, key, draw):
        """An R8 file of a shape OpenCV draws anti-aliased (ring, star, stroke): compose draws
        the same calls in the layer's colour, so coverage x colour is the CPU build's picture."""
        hit = self.sprites.get(key)
        if hit is None:
            w, h = key[-2], key[-1]
            im = np.zeros((h, w), np.uint8)
            draw(im)
            path = os.path.join(self.tmp, hashlib.sha1(repr(key).encode()).hexdigest() + ".r8")
            with open(path, "wb") as f:
                f.write(im.tobytes())
            hit = self.sprites[key] = path
        return hit

    def _masked(self, out, l, w, h):
        """Add the layer's shape mask, when compose would use one (not a plain rect)."""
        shape, radius = l.get("shape", "rect"), l.get("radius", 0)
        if not (shape == "rect" and not radius):
            out["mask"] = self._mask(w, h, shape, radius)
        return out

    def layer(self, l):
        """One plan layer -> pugrec-gpu's form, or None (nothing to draw)."""
        ox = oy = 0
        clip = None
        self.VW, self.VH = self.W, self.H
        if l.get("clip"):   # as compose.paint: the clip rect's view of the canvas, layer shifted into it
            cx, cy, cw, ch = l["clip"]
            x0, y0 = max(0, int(round(cx))), max(0, int(round(cy)))
            x1, y1 = min(self.W, int(round(cx + cw))), min(self.H, int(round(cy + ch)))
            if x1 <= x0 or y1 <= y0:
                return None
            clip, ox, oy = [x0, y0, x1, y1], x0, y0
            self.VW, self.VH = x1 - x0, y1 - y0
            l = compose._shifted(l, -x0, -y0)
        out = self._layer(l)
        if out is None:
            return None
        out["box"][0] += ox
        out["box"][1] += oy
        if clip:
            out["clip"] = clip
        return out

    def _layer(self, l):
        t = l["type"]
        op = l.get("opacity", 1.0)
        if t == "video":
            s = self.ids.get(l["src"])
            if s is None:
                return None
            fw, fh = self.plan["streams"][l["src"]]["size"]
            dx, dy, dw, dh = l["dst"]
            x, y = int(round(dx)), int(round(dy))
            w, h = max(1, int(round(dx + dw)) - x), max(1, int(round(dy + dh)) - y)
            out = self._masked({"k": "video", "s": s, "box": [x, y, w, h], "op": op, "dim": l.get("dim") or 0}, l, w, h)
            if l.get("blur"):
                sw, sh = max(1, w // 4), max(1, h // 4)
                out["blur"] = {"size": [sw, sh], "k": max(3, int(l["blur"] / 4) | 1),
                               "crop": crop_params(fw, fh, l["rect"], sw, sh)}
            else:
                out["crop"] = crop_params(fw, fh, l["rect"], w, h)
            if l.get("border"):
                bw, color = l["border"]
                shape, radius = l.get("shape", "rect"), l.get("radius", 0)
                ib = int(bw)
                out["border"] = [ib, *_rgb(color)]
                out["imask"] = self._mask(max(1, w - 2 * ib), max(1, h - 2 * ib), shape, max(0, radius - bw))
            return out
        if t in ("fill", "gradient"):
            dx, dy, dw, dh = l["dst"]
            x, y = int(round(dx)), int(round(dy))
            w, h = max(1, int(round(dw))), max(1, int(round(dh)))
            if t == "fill":
                return self._masked({"k": "fill", "box": [x, y, w, h], "op": op, "rgb": _rgb(l["color"])}, l, w, h)
            a = math.radians(l.get("angle", 180))
            ddx, ddy = math.sin(a), -math.cos(a)
            half = (abs(w * ddx) + abs(h * ddy)) / 2 or 1.0
            return self._masked({"k": "grad", "box": [x, y, w, h], "op": op,
                                 "stops": [_rgb(c) for c in l["colors"]], "dir": [ddx, ddy, half]}, l, w, h)
        if t == "shadow":
            dx, dy, dw, dh = l["dst"]
            blur, off = l.get("blur", 20.0), l.get("offset", 0.0)
            sigma = max(0.5, blur / 2)
            m = int(math.ceil(3 * sigma))
            w, h = max(1, int(round(dw))), max(1, int(round(dh)))
            radius = l.get("radius", 0)
            key = f"{w}x{h}r{int(radius)}s{round(sigma, 1)}"
            return {"k": "shadow", "box": [int(round(dx)) - m, int(round(dy + off)) - m, w + 2 * m, h + 2 * m],
                    "card": [w, h, m], "mask": self._mask(w, h, "round", radius), "sigma": sigma, "key": key,
                    "op": l.get("opacity", 0.3)}
        if t == "ring":
            r, lw = int(l["r"]), max(1, int(l["width"]))
            cx, cy = int(l["x"]), int(l["y"])
            x0, y0 = max(0, cx - r - lw), max(0, cy - r - lw)
            x1, y1 = min(self.VW, cx + r + lw + 1), min(self.VH, cy + r + lw + 1)
            if x1 <= x0 or y1 <= y0:
                return None
            key = ("ring", cx - x0, cy - y0, r, lw, x1 - x0, y1 - y0)
            path = self._coverage(key, lambda im: cv2.circle(im, (cx - x0, cy - y0), r, 255, lw, cv2.LINE_AA))
            return {"k": "fill", "box": [x0, y0, x1 - x0, y1 - y0], "mask": path, "rgb": _rgb(l["color"]), "op": op}
        if t == "text":
            key = ("text", round(l["size"]), tuple((w["w"], w["hi"]) for w in l["words"]), l["color"], l["hi"],
                   l["bg"], l["bg_opacity"], l.get("font"), l.get("weight"), l.get("pad"), l.get("radius"))
            path, w, h = self._sprite(key, lambda: compose.text_image(l))
            x = l["x"] if l.get("align") == "left" else l["x"] - w / 2
            return {"k": "sprite", "file": path, "box": [int(round(x)), int(round(l["y"] - h / 2)), w, h], "op": op}
        if t == "image":
            images = self.plan.get("images")
            if not images:
                return None
            dx, dy, dw, dh = l["dst"]
            w, h = max(1, int(round(dw))), max(1, int(round(dh)))
            file = images[l["img"]]["file"]
            pix, flip, rect = l.get("pixelated", False), l.get("flip", False), l.get("rect")
            key = ("image", file, w, h, pix, flip, tuple(rect) if rect else None)
            path, _, _ = self._sprite(key, lambda: compose.image_bgra(file, w, h, pix, flip, rect))
            return self._masked({"k": "sprite", "file": path, "box": [int(round(dx)), int(round(dy)), w, h], "op": op},
                                l, w, h)
        if t in ("star", "stroke"):
            if t == "star":
                r = l["r"]
                if r < 0.5:
                    return None
                x0, y0 = max(0, int(l["x"] - r) - 2), max(0, int(l["y"] - r) - 2)
                x1, y1 = min(self.VW, int(l["x"] + r) + 3), min(self.VH, int(l["y"] + r) + 3)
                pts = compose.star_points(l["x"], l["y"], r, l["rot"])
            else:
                pts = l["points"]
                if len(pts) < 2 or op <= 0:
                    return None
                lw = max(1, int(round(l["width"])))
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                x0, y0 = max(0, int(min(xs)) - lw - 2), max(0, int(min(ys)) - lw - 2)
                x1, y1 = min(self.VW, int(max(xs)) + lw + 3), min(self.VH, int(max(ys)) + lw + 3)
            if x1 <= x0 or y1 <= y0:
                return None
            arr = np.array([[(px - x0) * 16, (py - y0) * 16] for px, py in pts], np.int32)  # 1/16 px, as compose
            if t == "star":
                key = ("star", arr.tobytes(), x1 - x0, y1 - y0)
                path = self._coverage(key, lambda im: cv2.fillPoly(im, [arr], 255, cv2.LINE_AA, shift=4))
            else:
                closed = bool(l.get("closed"))
                key = ("stroke", arr.tobytes(), closed, lw, x1 - x0, y1 - y0)
                path = self._coverage(key, lambda im: cv2.polylines(im, [arr], closed, 255, lw, cv2.LINE_AA, shift=4))
            return {"k": "fill", "box": [x0, y0, x1 - x0, y1 - y0], "mask": path, "rgb": _rgb(l["color"]), "op": op}
        return None

    def lines(self, frames):
        """JSON lines for these frames: the header, then per run its decoders, then its frames."""
        fps = self.plan["output"]["fps"]
        yield json.dumps(self.header())
        for r0, r1, _src, st in compose.runs(frames, fps):
            run = frames[r0:r1]
            used = sorted({self.ids[l["src"]] for f in run for l in f["layers"]
                           if l["type"] == "video" and l["src"] in self.ids})
            yield json.dumps({"run": {"streams": used, "start": st, "hold": bool(run[0].get("hold"))}})
            for f in run:
                out = [x for x in (self.layer(l) for l in f["layers"]) if x is not None]
                yield json.dumps({"f": out}, separators=(",", ":"))


def encoder_cmd(W, H, fps, path, encoder="gpu", crf=18, preset="medium"):
    """ffmpeg reading pugrec-gpu's NV12 frames: h264_vaapi (CQP; qp = crf + 2, measured to give
    about x264's size at crf 18) or libx264 as compose.py uses it."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", "-"]
    if encoder == "gpu":
        cmd += ["-vaapi_device", RENDER_NODE, "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                "-rc_mode", "CQP", "-qp", str(int(crf) + 2)]
    else:
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    return cmd + ["-f", "matroska", path]


def render_range(plan, i0, i1, path, encoder="gpu", crf=18, preset="medium", counter=None, tmp=None):
    """Paint plan frames [i0, i1) on the GPU into a video-only file. Returns the exit code
    (99 = stopped at the GPU memory limit; the caller falls back to compose.py)."""
    exe = binary()
    if not exe:
        return 127
    W, H = plan["output"]["size"]
    fps = plan["output"]["fps"]
    tmp = tmp or os.path.dirname(os.path.abspath(path))
    sprite_dir = os.path.join(tmp, f"sprites-{i0}")
    os.makedirs(sprite_dir, exist_ok=True)
    job = Job(plan, sprite_dir)
    gpu = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    enc = subprocess.Popen(encoder_cmd(W, H, fps, path, encoder, crf, preset), stdin=gpu.stdout)
    gpu.stdout.close()   # the encoder owns the read end now
    done = [0]

    def progress():
        for raw in gpu.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line.startswith("p ") and line[2:].isdigit():
                n = int(line[2:])
                if counter is not None:
                    with counter.get_lock():
                        counter.value += n - done[0]
                done[0] = n
            elif line:
                print(line, file=sys.stderr)

    reader = threading.Thread(target=progress, daemon=True)
    reader.start()
    try:
        for line in job.lines(plan["frames"][i0:i1]):
            gpu.stdin.write(line.encode() + b"\n")
        gpu.stdin.close()
    except BrokenPipeError:
        pass
    rc = gpu.wait()
    reader.join()
    erc = enc.wait()
    shutil.rmtree(sprite_dir, ignore_errors=True)
    if counter is not None and rc != 0:   # the caller redoes this chunk: take its frames back
        with counter.get_lock():
            counter.value -= done[0]
    return rc or erc
