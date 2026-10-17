"""The build compositor: draws a resolved plan's layers, frame by frame, into an MP4.

Dumb on purpose - every decision is in the plan (resolve.py); this only decodes the
streams the plan names at the times it names, and paints. The browser preview
paints the same layers, so they match.
"""

import fcntl
import math
import os
import subprocess
import sys

import cv2
import numpy as np

from ..render import _read_exact


def hex_bgr(h):
    h = h.lstrip("#")
    return np.float32([int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)])


class Decoder:
    """One stream, decoded at the output fps on the *session* clock from session time `start`:
    frame i is the stream at start + i/fps (padded with its first frame before it began)."""

    def __init__(self, stream, start, fps, n):
        self.w, self.h = stream["size"]
        off = stream.get("offset", 0.0)
        seek = max(0.0, start - off - 1.0)  # fast seek a little before, then exact via fps start_time
        vf = f"setpts=PTS+{off:.6f}/TB,fps={fps}:start_time={start:.6f}"
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{seek:.3f}", "-copyts", "-i", stream["file"], "-vf", vf,
             "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, bufsize=0)
        try:  # 1 MB pipe (Linux default max): a 5K frame in ~32 reads instead of ~500
            fcntl.fcntl(self.proc.stdout.fileno(), 1031, 1 << 20)  # F_SETPIPE_SZ
        except OSError:
            pass
        self.last = None

    def next(self):
        raw = _read_exact(self.proc.stdout, self.w * self.h * 3)
        if raw is not None:
            self.last = np.frombuffer(raw, np.uint8).reshape(self.h, self.w, 3)
        return self.last  # stream ended early: hold its last frame

    def close(self):
        self.proc.stdout.close()
        self.proc.kill()
        self.proc.wait()


_masks = {}


def mask(w, h, shape, radius):
    """Anti-aliased 0..1 mask (h, w, 1) for a shape, cached."""
    key = (w, h, shape, int(radius))
    m = _masks.get(key)
    if m is None:
        ss = 4
        big = np.zeros((h * ss, w * ss), np.uint8)
        if shape == "circle":
            cv2.ellipse(big, (w * ss // 2, h * ss // 2), (w * ss // 2, h * ss // 2), 0, 0, 360, 255, -1, cv2.LINE_AA)
        else:  # rounded rect
            r = max(0, min(int(radius * ss), w * ss // 2, h * ss // 2))
            cv2.rectangle(big, (r, 0), (w * ss - 1 - r, h * ss - 1), 255, -1)
            cv2.rectangle(big, (0, r), (w * ss - 1, h * ss - 1 - r), 255, -1)
            for cx, cy in ((r, r), (w * ss - 1 - r, r), (r, h * ss - 1 - r), (w * ss - 1 - r, h * ss - 1 - r)):
                cv2.circle(big, (cx, cy), r, 255, -1, cv2.LINE_AA)
        m = cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)[..., None] / 255
        _masks[key] = m
        if len(_masks) > 64:
            _masks.pop(next(iter(_masks)))
    return m


def crop_scaled(frame, rect, ow, oh):
    """Sub-pixel crop of rect (source px) scaled to ow x oh: integer crop + area resample,
    then the fractional remainder as a translation (sharp text, no jitter on slow pans)."""
    H, W = frame.shape[:2]
    x0f, y0f, vw, vh = rect
    sx, sy = vw / ow, vh / oh
    x0, y0 = max(0, int(math.floor(x0f)) - 1), max(0, int(math.floor(y0f)) - 1)
    x1, y1 = min(W, int(math.ceil(x0f + vw)) + 2), min(H, int(math.ceil(y0f + vh)) + 2)
    crop = frame[y0:y1, x0:x1]
    cw, ch = max(1, round((x1 - x0) / sx)), max(1, round((y1 - y0) / sy))
    interp = cv2.INTER_AREA if sx >= 1 else cv2.INTER_LINEAR
    small = cv2.resize(crop, (cw, ch), interpolation=interp)
    kx, ky = cw / (x1 - x0), ch / (y1 - y0)
    M = np.float32([[1, 0, -(x0f - x0) * kx], [0, 1, -(y0f - y0) * ky]])
    return cv2.warpAffine(small, M, (ow, oh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def blit(canvas, img, x, y, alpha=None, opacity=1.0):
    """Put img (h, w, 3 uint8) into the uint8 canvas at (x, y), clipped to the canvas.
    alpha: (h, w, 1) float mask or None (= opaque). Everything stays uint8: opaque layers
    are a plain copy, blends run in OpenCV (multithreaded) - a float canvas cost ~48 ms/frame."""
    H, W = canvas.shape[:2]
    h, w = img.shape[:2]
    cx0, cy0 = max(0, x), max(0, y)
    cx1, cy1 = min(W, x + w), min(H, y + h)
    if cx1 <= cx0 or cy1 <= cy0 or opacity <= 0.001:
        return
    sx0, sy0 = cx0 - x, cy0 - y
    src = np.ascontiguousarray(img[sy0:sy0 + cy1 - cy0, sx0:sx0 + cx1 - cx0])
    roi = canvas[cy0:cy1, cx0:cx1]
    if alpha is None:
        if opacity >= 0.999:
            roi[...] = src
        else:
            roi[...] = cv2.addWeighted(src, opacity, roi, 1 - opacity, 0)
        return
    wa = np.ascontiguousarray(alpha[sy0:sy0 + cy1 - cy0, sx0:sx0 + cx1 - cx0, 0])
    if opacity < 0.999:
        wa = wa * np.float32(opacity)
    roi[...] = cv2.blendLinear(src, np.ascontiguousarray(roi), wa, 1 - wa)


_solid = {}


def solid(w, h, color):
    key = (w, h, color)
    s = _solid.get(key)
    if s is None:
        s = np.empty((h, w, 3), np.uint8)
        s[...] = hex_bgr(color).astype(np.uint8)
        _solid[key] = s
        if len(_solid) > 32:
            _solid.pop(next(iter(_solid)))
    return s


def draw_video(canvas, frame, l):
    dx, dy, dw, dh = l["dst"]
    x, y = int(round(dx)), int(round(dy))
    w, h = max(1, int(round(dx + dw)) - x), max(1, int(round(dy + dh)) - y)
    if l.get("blur"):
        # Blur at quarter size, then up: same look, a fraction of the cost.
        small = crop_scaled(frame, l["rect"], max(1, w // 4), max(1, h // 4))
        k = max(3, int(l["blur"] / 4) | 1)
        small = cv2.GaussianBlur(small, (k, k), 0)
        img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        img = crop_scaled(frame, l["rect"], w, h)
    if l.get("dim"):
        img = cv2.convertScaleAbs(img, alpha=1 - l["dim"])
    shape = l.get("shape", "rect")
    a = None if shape == "rect" and not l.get("radius") else mask(w, h, shape, l.get("radius", 0))
    op = l.get("opacity", 1.0)
    blit(canvas, img, x, y, a, op)
    if l.get("border"):
        bw, color = l["border"]
        ib = int(bw)
        outer = a if a is not None else np.ones((h, w, 1), np.float32)
        inner = mask(max(1, w - 2 * ib), max(1, h - 2 * ib), shape, max(0, l.get("radius", 0) - bw))
        ring = outer.copy()
        ring[ib:ib + inner.shape[0], ib:ib + inner.shape[1]] -= inner
        np.clip(ring, 0, 1, out=ring)
        blit(canvas, solid(w, h, color), x, y, ring, op)


def draw_fill(canvas, l):
    dx, dy, dw, dh = l["dst"]
    x, y = int(round(dx)), int(round(dy))
    w, h = max(1, int(round(dw))), max(1, int(round(dh)))
    shape = l.get("shape", "rect")
    a = None if shape == "rect" and not l.get("radius") else mask(w, h, shape, l.get("radius", 0))
    blit(canvas, solid(w, h, l["color"]), x, y, a, l.get("opacity", 1.0))


_gradients = {}


def gradient_t(w, h, angle):
    """Position 0..1 along a CSS-style linear gradient (0 deg = towards the top, 90 = right),
    whose line runs through the centre and just reaches the corners - the same maths as the
    preview's createLinearGradient."""
    a = math.radians(angle)
    dx, dy = math.sin(a), -math.cos(a)
    half = (abs(w * dx) + abs(h * dy)) / 2 or 1.0
    xs = np.arange(w, dtype=np.float32) + 0.5 - w / 2
    ys = np.arange(h, dtype=np.float32)[:, None] + 0.5 - h / 2
    return np.clip((xs * dx + ys * dy) / (2 * half) + 0.5, 0, 1)


def gradient(w, h, colors, angle):
    key = (w, h, tuple(colors), round(angle, 2))
    g = _gradients.get(key)
    if g is None:
        t = gradient_t(w, h, angle)
        stops = np.linspace(0, 1, len(colors))
        cols = np.stack([hex_bgr(c) for c in colors])  # (n, 3) BGR
        g = np.stack([np.interp(t, stops, cols[:, i]) for i in range(3)], axis=-1)
        g = np.clip(g + 0.5, 0, 255).astype(np.uint8)
        _gradients[key] = g
        if len(_gradients) > 8:
            _gradients.pop(next(iter(_gradients)))
    return g


def draw_gradient(canvas, l):
    dx, dy, dw, dh = l["dst"]
    x, y = int(round(dx)), int(round(dy))
    w, h = max(1, int(round(dw))), max(1, int(round(dh)))
    shape = l.get("shape", "rect")
    a = None if shape == "rect" and not l.get("radius") else mask(w, h, shape, l.get("radius", 0))
    blit(canvas, gradient(w, h, l["colors"], l.get("angle", 180)), x, y, a, l.get("opacity", 1.0))


_shadows = {}


def draw_shadow(canvas, l):
    """A soft black shadow of a rounded rect: Gaussian sigma = blur / 2 (as canvas shadowBlur),
    moved down by `offset` px. Only the shadow is drawn; the card goes on top of it."""
    dx, dy, dw, dh = l["dst"]
    blur, off = l.get("blur", 20.0), l.get("offset", 0.0)
    sigma = max(0.5, blur / 2)
    m = int(math.ceil(3 * sigma))
    w, h = max(1, int(round(dw))), max(1, int(round(dh)))
    key = (w, h, int(l.get("radius", 0)), round(sigma, 1))
    a = _shadows.get(key)
    if a is None:
        a = np.zeros((h + 2 * m, w + 2 * m, 1), np.float32)
        a[m:m + h, m:m + w] = mask(w, h, "round", l.get("radius", 0))
        a = cv2.GaussianBlur(a, (0, 0), sigma)[..., None]
        _shadows[key] = a
        if len(_shadows) > 16:
            _shadows.pop(next(iter(_shadows)))
    x, y = int(round(dx)) - m, int(round(dy + off)) - m
    blit(canvas, solid(a.shape[1], a.shape[0], "#000000"), x, y, a, l.get("opacity", 0.3))


def draw_ring(canvas, l):
    """Only the ring's own square is touched (a full-frame copy per ring was wasteful)."""
    H, W = canvas.shape[:2]
    r, lw = int(l["r"]), max(1, int(l["width"]))
    cx, cy = int(l["x"]), int(l["y"])
    x0, y0 = max(0, cx - r - lw), max(0, cy - r - lw)
    x1, y1 = min(W, cx + r + lw + 1), min(H, cy + r + lw + 1)
    if x1 <= x0 or y1 <= y0:
        return
    roi = canvas[y0:y1, x0:x1]
    over = roi.copy()
    color = tuple(int(c) for c in hex_bgr(l["color"]))
    cv2.circle(over, (cx - x0, cy - y0), r, color, lw, cv2.LINE_AA)
    a = l.get("opacity", 1.0)
    roi[...] = cv2.addWeighted(over, a, roi, 1 - a, 0)


FONT = "/usr/share/fonts/ibm-plex-sans-fonts/IBMPlexSans-SemiBold.otf"   # if fontconfig finds nothing
_fonts, _texts, _font_files = {}, {}, {}


def font_file(family, weight):
    """A font file for family + CSS weight, via fontconfig (what the browser preview uses too)."""
    key = (family, weight)
    if key not in _font_files:
        fc_weight = {100: 0, 200: 40, 300: 50, 400: 80, 500: 100, 600: 180, 700: 200, 800: 205, 900: 210}
        w = fc_weight.get(round(weight / 100) * 100, 80)
        try:
            r = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:weight={w}"], capture_output=True,
                               text=True, timeout=5)
            _font_files[key] = r.stdout.strip() or FONT
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _font_files[key] = FONT
    return _font_files[key]


def _font(size, family="IBM Plex Sans", weight=600):
    from PIL import ImageFont
    key = (size, family, weight)
    f = _fonts.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(font_file(family, weight), size)
        except OSError:
            f = ImageFont.load_default(size)
        _fonts[key] = f
    return f


_images = {}


def image_bgra(path, w, h, pixelated=False, flip=False, rect=None):
    """(BGR uint8, alpha (h, w, 1)) of an image file scaled to w x h (cached). pixelated =
    nearest-neighbour (pixel art stays crisp); flip = mirrored left-right; rect = the part of
    the image to use (image px), e.g. a backdrop's cover crop."""
    key = (path, w, h, pixelated, flip, tuple(rect) if rect else None)
    hit = _images.get(key)
    if hit is None:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGBA")
            if rect:
                sx, sy, sw, sh = rect
                im = im.crop((int(round(sx)), int(round(sy)), int(round(sx + sw)), int(round(sy + sh))))
            if flip:
                im = im.transpose(Image.FLIP_LEFT_RIGHT)
            a = np.asarray(im.resize((w, h), Image.NEAREST if pixelated else Image.LANCZOS), np.uint8)
        hit = (np.ascontiguousarray(a[..., 2::-1]), a[..., 3:4].astype(np.float32) / 255)
        _images[key] = hit
        if len(_images) > 16:
            _images.pop(next(iter(_images)))
    return hit


def draw_image(canvas, l, images):
    dx, dy, dw, dh = l["dst"]
    w, h = max(1, int(round(dw))), max(1, int(round(dh)))
    bgr, alpha = image_bgra(images[l["img"]]["file"], w, h, l.get("pixelated", False), l.get("flip", False),
                            l.get("rect"))
    shape = l.get("shape", "rect")
    if shape != "rect" or l.get("radius"):
        alpha = alpha * mask(w, h, shape, l.get("radius", 0))
    blit(canvas, bgr, int(round(dx)), int(round(dy)), alpha, l.get("opacity", 1.0))


def star_points(x, y, r, rot, inner=0.28):
    """The 8 corners of a 4-point sparkle star (the same maths as player.js)."""
    pts = []
    for i in range(8):
        a = rot + i * math.pi / 4
        rr_ = r if i % 2 == 0 else r * inner
        pts.append((x + rr_ * math.sin(a), y - rr_ * math.cos(a)))
    return pts


def draw_star(canvas, l):
    """A 4-point star, anti-aliased, touching only its own square."""
    H, W = canvas.shape[:2]
    r = l["r"]
    if r < 0.5:
        return
    x0, y0 = max(0, int(l["x"] - r) - 2), max(0, int(l["y"] - r) - 2)
    x1, y1 = min(W, int(l["x"] + r) + 3), min(H, int(l["y"] + r) + 3)
    if x1 <= x0 or y1 <= y0:
        return
    roi = canvas[y0:y1, x0:x1]
    over = roi.copy()
    pts = np.array([[(px - x0) * 16, (py - y0) * 16] for px, py in star_points(l["x"], l["y"], r, l["rot"])], np.int32)
    color = tuple(int(c) for c in hex_bgr(l["color"]))
    cv2.fillPoly(over, [pts], color, cv2.LINE_AA, shift=4)  # shift=4: 1/16 px precision
    a = l.get("opacity", 1.0)
    roi[...] = cv2.addWeighted(over, a, roi, 1 - a, 0)


def _shifted(l, dx, dy):
    """A layer moved by (dx, dy) - for drawing a clipped layer onto a sub-view of the canvas."""
    m = dict(l)
    if "dst" in m:
        m["dst"] = [m["dst"][0] + dx, m["dst"][1] + dy, m["dst"][2], m["dst"][3]]
    if "x" in m:
        m["x"], m["y"] = m["x"] + dx, m["y"] + dy
    return m


def text_image(l):
    """(BGR uint8, alpha (h, w, 1)) for a caption line: pill background + words, the
    spoken one highlighted. Same geometry as player.js: height size*1.5, pad each side."""
    from PIL import Image, ImageDraw
    size = max(6, int(round(l["size"])))
    family, weight = l.get("font", "IBM Plex Sans"), l.get("weight", 600)
    key = (size, tuple((w["w"], w["hi"]) for w in l["words"]), l["color"], l["hi"], l["bg"], l["bg_opacity"],
           family, weight, l.get("pad"), l.get("radius"))
    hit = _texts.get(key)
    if hit:
        return hit
    font = _font(size, family, weight)
    space = font.getlength(" ")
    widths = [font.getlength(w["w"]) for w in l["words"]]
    pad = int(round(l["pad"]))
    tw = sum(widths) + space * (len(widths) - 1)
    W, H = int(tw + 2 * pad), int(size * 1.5)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    bg = tuple(int(c) for c in hex_bgr(l["bg"])[::-1]) + (int(255 * l["bg_opacity"]),)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=int(l["radius"]), fill=bg)
    x = pad
    for w, wd in zip(l["words"], widths):
        col = l["hi"] if w["hi"] else l["color"]
        d.text((x, H / 2), w["w"], font=font, fill=tuple(int(c) for c in hex_bgr(col)[::-1]) + (255,), anchor="lm")
        x += wd + space
    a = np.asarray(im, np.uint8)
    bgr = np.ascontiguousarray(a[..., 2::-1])
    alpha = a[..., 3:4].astype(np.float32) / 255
    _texts[key] = (bgr, alpha)
    if len(_texts) > 256:
        _texts.pop(next(iter(_texts)))
    return bgr, alpha


def draw_text(canvas, l):
    bgr, alpha = text_image(l)
    h, w = bgr.shape[:2]
    x = l["x"] if l.get("align") == "left" else l["x"] - w / 2
    blit(canvas, bgr, int(round(x)), int(round(l["y"] - h / 2)), alpha, l.get("opacity", 1.0))


def draw_stroke(canvas, l):
    """A polyline (a callout's border, part drawn), anti-aliased, touching only its own box."""
    H, W = canvas.shape[:2]
    pts = l["points"]
    if len(pts) < 2 or l.get("opacity", 1.0) <= 0:
        return
    lw = max(1, int(round(l["width"])))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0 = max(0, int(min(xs)) - lw - 2), max(0, int(min(ys)) - lw - 2)
    x1, y1 = min(W, int(max(xs)) + lw + 3), min(H, int(max(ys)) + lw + 3)
    if x1 <= x0 or y1 <= y0:
        return
    roi = canvas[y0:y1, x0:x1]
    over = roi.copy()
    arr = np.array([[(px - x0) * 16, (py - y0) * 16] for px, py in pts], np.int32)
    color = tuple(int(c) for c in hex_bgr(l["color"]))
    cv2.polylines(over, [arr], bool(l.get("closed")), color, lw, cv2.LINE_AA, shift=4)  # 1/16 px precision
    a = l.get("opacity", 1.0)
    roi[...] = cv2.addWeighted(over, a, roi, 1 - a, 0)


def paint(canvas, layers, frames, images=None):
    canvas[...] = 0
    full = canvas
    for l in layers:
        canvas = full
        if l.get("clip"):  # draw onto the clip rect's view of the canvas: its edges do the clipping
            cx, cy, cw, ch = l["clip"]
            x0, y0 = max(0, int(round(cx))), max(0, int(round(cy)))
            x1, y1 = min(full.shape[1], int(round(cx + cw))), min(full.shape[0], int(round(cy + ch)))
            if x1 <= x0 or y1 <= y0:
                continue
            canvas = full[y0:y1, x0:x1]
            l = _shifted(l, -x0, -y0)
        if l["type"] == "video":
            f = frames.get(l["src"])
            if f is not None:
                draw_video(canvas, f, l)
        elif l["type"] == "fill":
            draw_fill(canvas, l)
        elif l["type"] == "gradient":
            draw_gradient(canvas, l)
        elif l["type"] == "shadow":
            draw_shadow(canvas, l)
        elif l["type"] == "ring":
            draw_ring(canvas, l)
        elif l["type"] == "text":
            draw_text(canvas, l)
        elif l["type"] == "image" and images:
            draw_image(canvas, l, images)
        elif l["type"] == "star":
            draw_star(canvas, l)
        elif l["type"] == "stroke":
            draw_stroke(canvas, l)


def runs(frames, fps):
    """Split frames into runs of continuous source time: [(i0, i1, src, st0)]. Within a run
    every frame is the previous one + 1/fps in the same source (one decoder, one audio piece)."""
    out, i0 = [], 0
    dt = 1 / fps
    for i in range(1, len(frames) + 1):
        # A hold (a callout's pause) is a run of its own: the same source moment on every frame.
        step = 0.0 if i < len(frames) and frames[i].get("hold") else dt
        if i == len(frames) or frames[i]["src"] != frames[i - 1]["src"] or \
                bool(frames[i].get("hold")) != bool(frames[i - 1].get("hold")) or \
                abs(frames[i]["st"] - frames[i - 1]["st"] - step) > 1e-3:
            out.append((i0, i, frames[i0]["src"], frames[i0]["st"]))
            i0 = i
    return out


def audio_args(plan, frames, first_input):
    """ffmpeg inputs + filter for the audio under exactly these frames (10 ms fades at splices)."""
    files = {a["source"]: a["file"] for a in plan["audio"]}
    fps = plan["output"]["fps"]
    # A hold is silence: a piece with no source file (like a title card).
    pieces = [("hold" if frames[i0].get("hold") else src, st, st + (i1 - i0) / fps)
              for i0, i1, src, st in runs(frames, fps)]
    if not any(p[0] in files for p in pieces):
        return [], None
    inputs, idx = [], {}
    for src in dict.fromkeys(p[0] for p in pieces if p[0] in files):
        idx[src] = first_input + len(inputs) // 2
        inputs += ["-i", files[src]]
    parts, labels = [], []
    same = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono"  # concat needs one format
    for i, (src, a, b) in enumerate(pieces):
        d = b - a
        if src not in files:  # a title card (or a source without audio): silence, so sync holds
            parts.append(f"anullsrc=r=48000:cl=mono,atrim=duration={d:.4f},{same}[a{i}]")
        else:
            fade = min(0.01, d / 4)
            parts.append(f"[{idx[src]}:a:0]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d={fade:.3f},afade=t=out:st={max(0, d - fade):.4f}:d={fade:.3f},{same}[a{i}]")
        labels.append(f"[a{i}]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    return inputs, ";".join(parts)


_PLAN = None     # set before forking workers (fork shares it copy-on-write)
_COUNTER = None  # shared frame counter: inherited by the fork (it can't be pickled into map args)


def _render_range(args):
    """Worker: paint frames[i0:i1] of _PLAN into a video-only file."""
    i0, i1, path, crf, preset = args
    plan, counter = _PLAN, _COUNTER
    W, H = plan["output"]["size"]
    fps = plan["output"]["fps"]
    frames = plan["frames"][i0:i1]
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                            "-pix_fmt", "yuv420p", "-f", "matroska", path], stdin=subprocess.PIPE)
    canvas = np.zeros((H, W, 3), np.uint8)
    try:
        for r0, r1, _src, st in runs(frames, fps):
            run = frames[r0:r1]
            used = {l["src"] for f in run for l in f["layers"] if l["type"] == "video"}
            held = bool(run[0].get("hold"))   # one picture, decoded once, under every frame of the hold
            decs = {s: Decoder(plan["streams"][s], st, fps, 1 if held else len(run)) for s in used}
            still = {s: d.next() for s, d in decs.items()} if held else None
            try:
                for f in run:
                    paint(canvas, f["layers"], still or {s: d.next() for s, d in decs.items()}, plan.get("images"))
                    enc.stdin.write(canvas.data)
                    if counter is not None:
                        with counter.get_lock():
                            counter.value += 1
            finally:
                for d in decs.values():
                    d.close()
    finally:
        enc.stdin.close()
        rc = enc.wait()
    return rc


def build(plan, out_path, crf=18, preset="medium", progress=True, jobs=4):
    """Paint the plan into out_path (MP4). The plan has every frame's layers, so chunks are
    independent: `jobs` processes each render a range, then they're joined without
    re-encoding and the audio is muxed in once."""
    import multiprocessing as mp
    import tempfile
    import time
    global _PLAN, _COUNTER
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)  # before rendering, not at the mux
    fps = plan["output"]["fps"]
    frames = plan["frames"]
    n = len(frames)
    jobs = max(1, min(jobs, n // (fps * 2) or 1))  # chunks of at least ~2 s
    bounds = [round(i * n / jobs) for i in range(jobs + 1)]
    _PLAN = plan
    with tempfile.TemporaryDirectory(prefix="pugrec-build-") as tmp:
        parts = [os.path.join(tmp, f"part{i:03d}.mkv") for i in range(jobs)]
        ctx = mp.get_context("fork")
        counter = _COUNTER = ctx.Value("i", 0)
        work = [(bounds[i], bounds[i + 1], parts[i], crf, preset) for i in range(jobs)]
        t0 = time.monotonic()
        with ctx.Pool(jobs) as pool:
            res = pool.map_async(_render_range, work)
            while not res.ready():
                res.wait(1)
                if progress:
                    el = time.monotonic() - t0
                    print(f"\rbuild {plan['output']['name']}: {counter.value / fps:6.1f}/{n / fps:.1f} s "
                          f"({counter.value / el if el else 0:.0f} fps, {jobs} jobs)", end="", file=sys.stderr)
            rcs = res.get()
        if any(rcs):
            print(f"\nbuild: a chunk failed (exit codes {rcs})", file=sys.stderr)
            return 1
        lst = os.path.join(tmp, "parts.txt")
        with open(lst, "w") as f:
            # Exact durations: Matroska doesn't store the last frame's, so concat would drop it.
            for i, p in enumerate(parts):
                f.write(f"file '{p}'\nduration {(bounds[i + 1] - bounds[i]) / fps:.6f}\n")
        a_inputs, a_filter = audio_args(plan, frames, 1)
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, *a_inputs]
        if a_filter:
            cmd += ["-filter_complex", a_filter, "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "copy", "-movflags", "+faststart", "-shortest", out_path]
        rc = subprocess.run(cmd).returncode
    if progress:
        el = time.monotonic() - t0
        print(f"\rbuild {plan['output']['name']}: {n} frames in {el:.0f}s ({n / el:.0f} fps, {jobs} jobs) -> "
              f"{out_path}" + ("" if rc == 0 else f" (exit {rc})"), file=sys.stderr)
    return rc
