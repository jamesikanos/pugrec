"""edit.yaml + sessions -> a plan: per output frame, the layers to draw.

The plan is the only thing the compositors (browser preview, build) see, so they
can't disagree. Pure: no video is decoded here. Layers:

  video  {src: "main/screen", t: <source s>, rect: [x,y,w,h] source px,
          dst: [x,y,w,h] output px, shape: rect|round|circle, radius, blur, dim, opacity, border}
  fill   {dst, color, shape, radius, opacity}
  gradient {dst, colors: [2-4], angle (CSS deg: 0 up, 90 right), shape, radius, opacity}   (backdrop)
  shadow {dst, radius, blur (= 2 sigma, canvas shadowBlur), offset (px down), opacity}  (under a card)
  ring   {x, y, r, width, color, opacity}            (click highlight)
  text   {x, y (centre), size, words: [{w, hi}], color, hi, bg, bg_opacity, font, weight, pad, radius, opacity}
  image  {img: <id in plan["images"]>, dst, opacity, pixelated, flip, rect (crop, image px), shape, radius}
         (logo, card icon, sweep sprite, backdrop image)
  star   {x, y, r, rot, color, opacity}               (a 4-point sparkle)
Any layer may carry `clip: [x, y, w, h]` (output px): drawn only inside it (the sweep's reveal),
and `bg: true`: a background (blur copy, colour, backdrop, shadow) - transitions cross-fade it,
the spotlight and `move` never treat it as the view. Compositors ignore the flag.
Title cards are frames with src "card:intro"/"card:outro" (no video, silence underneath).

Times in a plan: output seconds for frames, *source* seconds in layers (`t`), so
every layer says which source and which moment of it - splicing several
sources later needs nothing new in the compositors.
"""

import bisect
import math
import os

from .. import faces as faces_mod
from .. import jsonl
from .. import transcript as transcript_mod
from ..render import Camera as AutoCamera
from ..timeline import Timeline
from . import script as script_mod

# Colours come from the style (script.DEFAULT_STYLE = the PugPal look, or a theme).


def last_video_pts(path):
    return last_pts(path, "v:0")


def last_pts(path, stream="v:0"):
    """pts (s) of the file's last packet of `stream` (v:0, a:0): reads only the last two
    minutes if that has packets, the whole file otherwise. None if ffprobe can't tell."""
    import subprocess

    def probe(*extra):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, *extra,
                            "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        ts = [float(x) for x in r.stdout.split() if x.replace(".", "", 1).isdigit()]
        return max(ts) if ts else None

    if not os.path.exists(path):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        dur = None
    end = probe("-read_intervals", f"{max(0.0, dur - 120):.3f}%") if dur else None
    return end if end is not None else probe()


_image_sizes = {}


def image_size(path):
    """(w, h) of an image file (Pillow, cached)."""
    if path not in _image_sizes:
        from PIL import Image
        with Image.open(path) as im:
            _image_sizes[path] = im.size
    return _image_sizes[path]
LOSS_FOR_CONTAIN = 0.15
BUBBLE_MOVE_S = 0.5
STIFFNESS = {"window": 6.0, "cursor": 4.0, "auto": 7.0, "camera": 2.5, "bubble": 2.5}


# ----------------------------------------------------------------- sources --
class Session:
    """One pugrec session: timeline, streams, derived tracks."""

    def __init__(self, path):
        self.path = path
        self.records = jsonl.read(os.path.join(path, "session.jsonl"))
        self.tl = Timeline(self.records)
        streams = {r["stream"]: r for r in self.records if r["type"] == "stream_start"}
        self.screen = streams["screen"]
        self.camera = streams.get("camera")
        if self.camera and not os.path.exists(os.path.join(path, self.camera["file"])):
            self.camera = None
        self.faces = faces_mod.load(path) if self.camera else None
        self.faces_t = [f[0] for f in self.faces] if self.faces else []
        self.duration = self.tl.duration
        self.cam_size = tuple(self.camera["pixels"]) if self.camera else None
        self.transcript = transcript_mod.load(path)
        # The recorder's countdown ends in a `start` marker: before it is warm-up (camera, password
        # prompt, countdown popups). Edits begin there by default. 0 for older recordings.
        mark = next((r for r in self.records if r["type"] == "marker" and r.get("name") == "start"), None)
        self.start = mark["t"] / 1e9 if mark else 0.0
        self._video_end = {}

    def video_end(self, kind):
        """Session time (s) of the last video packet in screen.mkv/camera.mkv, or None if
        unknown. The container duration can hide a stalled encoder (2026-10-13_19-20-50:
        video stopped at 63.8 s, audio and container ran to 94 s)."""
        if kind in self._video_end:
            return self._video_end[kind]
        rec = self.screen if kind == "screen" else self.camera
        end = None
        if rec:
            end = last_video_pts(os.path.join(self.path, rec["file"]))
            if end is not None and kind == "camera":
                end += rec["first_frame_t"] / 1e9
        self._video_end[kind] = end
        return end

    def face_at(self, t):
        """(cx, cy, h) of the face in camera fractions at t (interpolated), or None."""
        if not self.faces:
            return None
        i = bisect.bisect_left(self.faces_t, t)
        if i <= 0:
            return self.faces[0][1:]
        if i >= len(self.faces):
            return self.faces[-1][1:]
        a, b = self.faces[i - 1], self.faces[i]
        if b[0] - a[0] > 1.0:  # a gap in detection: hold the last face
            return a[1:]
        f = (t - a[0]) / (b[0] - a[0])
        return tuple(a[k] + (b[k] - a[k]) * f for k in (1, 2, 3))


PUNCT = ".,!?;:\"'()…"


def fix_word(w, fixes):
    """A one-word caption correction, case-insensitive, keeping punctuation ("akme," -> "Acme,")."""
    out = apply_fixes([(0.0, 0.0, w)], fixes)
    return out[0][2] if len(out) == 1 else " ".join(x[2] for x in out)


def apply_fixes(words, fixes):
    """Caption corrections over [(t, end, word)]: keys may be several words ("Field Pal":
    FieldPal) - those consecutive words become one, spanning their times. Case-insensitive;
    punctuation around the matched words is kept (the first word's before, the last's after)."""
    if not fixes:
        return words
    rules = sorted(((k.lower().split(), v) for k, v in fixes.items() if k.strip()), key=lambda r: -len(r[0]))
    cores = [w.strip(PUNCT).lower() for _, _, w in words]
    out, i = [], 0
    while i < len(words):
        for toks, repl in rules:
            n = len(toks)
            if cores[i:i + n] == toks and all(cores[i:i + n]):
                first, last = words[i][2], words[i + n - 1][2]
                lead = first[:len(first) - len(first.lstrip(PUNCT))]
                trail = last[len(last.rstrip(PUNCT)):]
                out.append((words[i][0], words[i + n - 1][1], lead + repl + trail))
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def caption_lines(segments, max_words=6, gap=0.6, lead=0.1, tail=0.4, fixes=None):
    """Transcript words -> caption lines [(t0, t1, [(t, end, word)])] in seconds. A line
    ends at sentence punctuation, a pause > gap, or max_words; it shows from just before
    its first word to a little after its last, never overlapping the next line."""
    words = apply_fixes([(w["t"] / 1e9, w["end"] / 1e9, w["w"]) for s in segments for w in s["words"]], fixes)
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if not nxt or len(cur) >= max_words or w[2][-1:] in ".?!" or nxt[0] - w[1] > gap:
            lines.append(cur)
            cur = []
    out = []
    for i, ln in enumerate(lines):
        t0 = max(0.0, ln[0][0] - lead)
        t1 = ln[-1][1] + tail
        if i + 1 < len(lines):
            t1 = min(t1, lines[i + 1][0][0] - lead)
        out.append((t0, max(t0 + 0.2, t1), ln))
    return out


# ------------------------------------------------------------------ geometry --
def expand_to_aspect(r, aspect):
    x, y, w, h = r
    if w / h < aspect:
        nw, nh = h * aspect, h
    else:
        nw, nh = w, w / aspect
    cx, cy = x + w / 2, y + h / 2
    return (cx - nw / 2, cy - nh / 2, nw, nh)


def clamp_rect(r, bw, bh):
    """Shift r inside [0,bw]x[0,bh]; if it's bigger than the bounds, centre it."""
    x, y, w, h = r
    x = (bw - w) / 2 if w >= bw else min(max(x, 0), bw - w)
    y = (bh - h) / 2 if h >= bh else min(max(y, 0), bh - h)
    return (x, y, w, h)


def cover_rect(target, aspect, bw, bh):
    """The aspect-shaped source rect showing as much of `target` as possible, inside the
    source. Returns (rect, loss): loss = fraction of the target that's cut off."""
    e = expand_to_aspect(target, aspect)
    x, y, w, h = e
    s = min(1.0, bw / w, bh / h)
    if s < 1.0:
        cx, cy = x + w / 2, y + h / 2
        w, h = w * s, h * s
        e = (cx - w / 2, cy - h / 2, w, h)
    e = clamp_rect(e, bw, bh)
    tx, ty, tw, th = target
    ix = max(0.0, min(tx + tw, e[0] + e[2]) - max(tx, e[0]))
    iy = max(0.0, min(ty + th, e[1] + e[3]) - max(ty, e[1]))
    return e, 1 - (ix * iy) / (tw * th) if tw * th else 0.0


def fit_inside(r_aspect, dst):
    """Largest rect of aspect r_aspect centred inside dst."""
    x, y, w, h = dst
    if w / h > r_aspect:
        nw, nh = h * r_aspect, h
    else:
        nw, nh = w, w / r_aspect
    return (x + (w - nw) / 2, y + (h - nh) / 2, nw, nh)


def inset_room(dst, padding):
    """The space a card may use inside a pane: the pane minus `padding` (fraction of its
    shorter side) on every side."""
    x, y, w, h = dst
    p = padding * min(w, h)
    return (x + p, y + p, max(1.0, w - 2 * p), max(1.0, h - 2 * p))


def place_in(card_w, card_h, room, position):
    """A card_w x card_h rect inside `room`, at `position` ([0..1, 0..1] of the free space)."""
    x, y, w, h = room
    return (x + (w - card_w) * position[0], y + (h - card_h) * position[1], card_w, card_h)


def push_amount(elapsed, over, ramp=1.0):
    """0 -> 1 progress of a push-in after `elapsed` s: speeds up over `ramp` s, steady, slows down
    over the last `ramp` s, done at `over` (a trapezoid velocity: a camera move, not an effect)."""
    if elapsed <= 0:
        return 0.0
    if elapsed >= over:
        return 1.0
    r = min(ramp, over / 2)
    v = 1 / (over - r)
    if elapsed < r:
        return 0.5 * v * elapsed * elapsed / r
    if elapsed < over - r:
        return 0.5 * v * r + v * (elapsed - r)
    return 1 - 0.5 * v * (over - elapsed) ** 2 / r


PUNCH_DIP, PUNCH_IN, PUNCH_OUT = 0.15, 0.3, 0.3   # s: anticipation, the punch, the release


def punch_amount(e, hold, dip):
    """Zoom progress of a punch-in `e` s after it starts: 0 -> -dip (a small pull back, the
    anticipation) -> 1 with a slight overshoot (it lands at PUNCH_DIP + PUNCH_IN) -> held -> eased
    back to 0. `dip` is relative to the zoom. Negative = wider than the view."""
    if e <= 0:
        return 0.0
    if e < PUNCH_DIP:
        return -dip * ease(e / PUNCH_DIP)
    e -= PUNCH_DIP
    if e < PUNCH_IN:
        u, c = e / PUNCH_IN - 1, 0.8              # easeOutBack, gentle: lands ~3% past, settles back
        return -dip + (1 + dip) * (1 + (c + 1) * u ** 3 + c * u ** 2)
    e -= PUNCH_IN + hold
    if e < 0:
        return 1.0
    return 1 - ease(e / PUNCH_OUT) if e < PUNCH_OUT else 0.0


def grow_rect(r, s, bw, bh):
    """r scaled by s about its centre; when s > 1, kept inside the source (bw x bh): moved in
    if it pokes out, capped (same aspect) if it can't fit - a view already showing the whole
    source has no room for the dip, so it's skipped there."""
    if s <= 1:
        return shrink_rect(r, s)
    s = max(1.0, min(s, bw / r[2], bh / r[3]))
    x, y, w, h = shrink_rect(r, s)
    if w <= bw and h <= bh:
        x, y = min(max(x, 0.0), bw - w), min(max(y, 0.0), bh - h)
    return (x, y, w, h)


def shrink_rect(r, s):
    """r scaled by s about its centre."""
    x, y, w, h = r
    return (x + w * (1 - s) / 2, y + h * (1 - s) / 2, w * s, h * s)


def pad_rect(r, frac):
    x, y, w, h = r
    p = frac * max(w, h)
    return (x - p, y - p, w + 2 * p, h + 2 * p)


def ease(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def lerp_rect(a, b, u):
    """Interpolate rects in centre + log-size space (zooms feel even)."""
    ac = (a[0] + a[2] / 2, a[1] + a[3] / 2, math.log(a[2]), math.log(a[3]))
    bc = (b[0] + b[2] / 2, b[1] + b[3] / 2, math.log(b[2]), math.log(b[3]))
    c = [ac[i] + (bc[i] - ac[i]) * u for i in range(4)]
    w, h = math.exp(c[2]), math.exp(c[3])
    return (c[0] - w / 2, c[1] - h / 2, w, h)


class Spring:
    """Critically damped spring on a rect (centre + log size)."""

    def __init__(self, rect, k):
        self.k = k
        self.pos = [rect[0] + rect[2] / 2, rect[1] + rect[3] / 2, math.log(rect[2]), math.log(rect[3])]
        self.vel = [0.0] * 4

    def step(self, rect, dt):
        goal = [rect[0] + rect[2] / 2, rect[1] + rect[3] / 2, math.log(rect[2]), math.log(rect[3])]
        for i in range(4):
            self.vel[i] += (self.k * self.k * (goal[i] - self.pos[i]) - 2 * self.k * self.vel[i]) * dt
            self.pos[i] += self.vel[i] * dt
        w, h = math.exp(self.pos[2]), math.exp(self.pos[3])
        return (self.pos[0] - w / 2, self.pos[1] - h / 2, w, h)


def pane_rects(kind_layout, ratio, W, H, gap):
    """Two dst rects for a split. row = side by side, column = stacked."""
    g = gap
    if kind_layout == "row":
        aw = W - 3 * g
        return [(g, g, aw * ratio, H - 2 * g), (2 * g + aw * ratio, g, aw * (1 - ratio), H - 2 * g)]
    ah = H - 3 * g
    return [(g, g, W - 2 * g, ah * ratio), (g, 2 * g + ah * ratio, W - 2 * g, ah * (1 - ratio))]


def corner_center(corner, d, margin, W, H):
    if isinstance(corner, list):
        return (corner[0] * W, corner[1] * H)
    x = margin + d / 2 if "left" in corner else W - margin - d / 2
    y = margin + d / 2 if corner.startswith("top") else H - margin - d / 2
    return (x, y)


def r1(v):
    return round(v, 1)


def rr(r):
    return [r1(v) for v in r]


# ------------------------------------------------------------------- shots --
class ShotEval:
    """Evaluates one pane's shot over consecutive frames (keeps smoothing state)."""

    def __init__(self, shot, sess, scene_at, aspect, warn):
        self.shot, self.s, self.aspect, self.warn = shot, sess, aspect, warn
        self.kind = shot["kind"]
        self.source = "camera" if self.kind == "camera" else "screen"
        self.spring = None
        self.frozen = None
        self.addr = None
        self.center = None  # cursor dead-zone centre
        if self.kind == "window":
            sel = shot["select"]
            if not sel.get("focused"):
                hit = sess.tl.find_window(sel, scene_at)
                if not hit:
                    warn(f"no window matches {sel} at {scene_at:.2f}s: showing the whole screen")
                    self.kind = "screen"
                else:
                    self.addr = hit[0]
        if self.kind == "auto":
            self.auto = AutoCamera(sess.tl, aspect, zoom=shot.get("zoom", 2.0))
        if self.source == "camera" and not sess.camera:
            warn("camera shot but the session has no camera: showing the whole screen")
            self.kind, self.source = "screen", "screen"

    def bounds(self):
        if self.source == "camera":
            return self.s.cam_size
        return (self.s.tl.W, self.s.tl.H)

    def raw_target(self, t):
        tl = self.s.tl
        k = self.kind
        if k == "screen":
            return (0.0, 0.0, float(tl.W), float(tl.H))
        if k == "rect":
            x, y, w, h = self.shot["rect"]
            if max(x, y, w, h) <= 1.0:  # fractions of the screen
                return (x * tl.W, y * tl.H, w * tl.W, h * tl.H)
            return (x, y, w, h)
        if k == "window":
            if self.shot["select"].get("focused"):
                hit = tl.find_window({"focused": True}, t)
                st = hit[1] if hit else None
            else:
                st = tl.window_state(self.addr, t)
            if not st:
                return self._last or (0.0, 0.0, float(tl.W), float(tl.H))
            r = tl.rect_px(st)
            if "region" in self.shot:
                fx, fy, fw, fh = self.shot["region"]
                r = (r[0] + fx * r[2], r[1] + fy * r[3], fw * r[2], fh * r[3])
                pad = self.shot.get("padding", 0.02)
            else:
                pad = self.shot.get("padding", 0.04)
            self._last = pad_rect(r, pad)
            return self._last
        if k == "cursor":
            maxw = min(tl.W, tl.H * self.aspect)
            w = maxw / self.shot["zoom"]
            h = w / self.aspect
            cx, cy = tl.cursor_at(t)
            if self.center is None:
                self.center = [cx, cy]
            dz = self.shot["dead_zone"]
            # Only move when the cursor leaves the middle (1 - 2*dz) of the view.
            for i, (c, half) in enumerate(((cx, w / 2), (cy, h / 2))):
                lim = half * (1 - 2 * dz)
                off = c - self.center[i]
                if abs(off) > lim:
                    self.center[i] += off - math.copysign(lim, off)
            return (self.center[0] - w / 2, self.center[1] - h / 2, w, h)
        if k == "auto":
            cx, cy, w = self.auto.target(t)
            h = w / self.aspect
            return (cx - w / 2, cy - h / 2, w, h)
        if k == "camera":
            cw, ch = self.s.cam_size
            f = self.s.face_at(t)
            if not f:
                return (0.0, 0.0, float(cw), float(ch))
            fcx, fcy, fh = f
            # Head and shoulders: ~3x the face height, a little headroom above the face.
            h = min(ch, fh * ch * 3.0 / self.shot.get("zoom", 1.0))
            w = h * self.aspect
            cy = fcy * ch + 0.15 * h
            return (fcx * cw - w / 2, cy - h / 2, w, h)
        raise AssertionError(k)

    _last = None

    def target(self, t, dt, first):
        """Smoothed target rect at t."""
        r = self.raw_target(t)
        # follow: false holds a window/camera where it was when the scene started (a still camera
        # frame instead of one that bobs with every lean - push can still tighten it).
        if self.kind in ("screen", "rect") or (self.kind in ("window", "camera") and not self.shot.get("follow", True)):
            if self.frozen is None:
                self.frozen = r
            return self.frozen
        if first or self.spring is None:
            self.spring = Spring(r, STIFFNESS.get(self.kind, 5.0))
            return r
        return self.spring.step(r, dt)


# ---------------------------------------------------------------- resolver --
class OutputResolver:
    def __init__(self, sc, out, sessions, warnings):
        self.sc, self.out, self.sessions = sc, out, sessions
        self.W, self.H = out["size"]
        self.unit = min(self.W, self.H) / 1080  # style px are at 1080p
        self.style = sc["style"]
        self.warnings = warnings
        self.scenes = script_mod.scenes_for_output(sc["scenes"], out["name"], self.style)
        self.evals = {}
        self.cur_scene = None
        self.prev = None          # (scene, layers fn state) during a transition
        self.trans_start = None
        # Output seconds, counted by frame() (once per output frame). Transitions and the bubble's
        # animation run on it, not on source time: a cut inside a fade (pauses: ...) jumps the
        # source clock and used to end the fade early - a visible pop.
        self.clock = -1.0
        self.frame_i = -1   # output frames counted by frame(); clock = frame_i * dt (no float drift)
        self.scene_t0 = {}  # scene index -> output clock when it (last) started: push-ins run on it
        self.punch_t0 = {}  # (scene index, punch i) -> output clock its anticipation started
        self.bubble_from = None
        self.bubble_cur = None
        self.bubble_spring = None
        self.spot_addr = {}  # scene index -> spotlit window address (resolved once per scene)
        self.images = {}     # image layers' files: id -> {file, size} (the plan's "images")
        cap = self.style["captions"]
        for i, s in enumerate(self.scenes):
            nxt = next((n for n in self.scenes[i + 1:] if n["source"] == s["source"]), None)
            if s["punch"] and s["push"]:
                self.warn(s, "punch and push together look restless: pick one for this scene")
            for p in s["punch"]:
                if nxt is not None and p["at"] >= nxt["at"]:
                    self.warn(s, f"punch at {p['at']} is after the scene ends ({nxt['at']}); ignored")
        self.lines = {name: caption_lines(sess.transcript or [], cap["max_words"], fixes=cap.get("fix")) for name, sess in sessions.items()}
        self.lines_t = {name: [l[0] for l in ls] for name, ls in self.lines.items()}

    def caption_layers(self, source, t):
        cap = self.style["captions"]
        if not cap["show"] or not self.lines[source]:
            return []
        i = bisect.bisect_right(self.lines_t[source], t) - 1
        if i < 0 or t > self.lines[source][i][1]:
            return []
        t0, t1, words = self.lines[source][i]
        size = cap["size"] * min(self.W, self.H)
        pos = cap["position"]
        if pos == "auto":
            pos = "bottom" if self.W >= self.H else "lower"
        y = {"bottom": self.H - 0.09 * self.H, "lower": 0.72 * self.H, "middle": self.H / 2, "top": 0.1 * self.H}[pos]
        out_words = []
        for k, (wt, we, w) in enumerate(words):
            nxt = words[k + 1][0] if k + 1 < len(words) else we
            out_words.append({"w": w, "hi": bool(cap["highlight"] and wt <= t < max(nxt, we))})
        fade = 0.12
        op = min(1.0, (t - t0) / fade, (t1 - t) / fade)
        return [{"type": "text", "x": r1(self.W / 2), "y": r1(y), "size": r1(size), "words": out_words,
                 "color": cap["color"], "hi": self.style["accent"], "bg": cap["background"],
                 "bg_opacity": cap["background_opacity"], "font": cap["font"], "weight": cap["weight"],
                 "pad": r1(0.45 * size), "radius": r1(self.style["radius"] * self.unit), "opacity": round(max(0.0, op), 3)}]

    # -- sparkle / sweep transitions ------------------------------------
    def _sparkle_colors(self):
        return self.style["sparkle"]["colors"] or ["#ffffff", self.style["accent"]]

    def _star(self, x, y, r, rot, color, local):
        k = math.sin(math.pi * local)  # grows, then fades away
        return {"type": "star", "x": r1(x), "y": r1(y), "r": r1(r * k), "rot": round(rot + local * math.pi / 2, 3),
                "color": color, "opacity": round(min(1.0, 1.3 * k), 3)}

    def sparkle_burst(self, seed, u):
        """4-point stars twinkling all over the frame during the transition (deterministic per
        transition, so preview and build draw the same ones)."""
        import random
        rng = random.Random(seed * 7919 + 1)
        s = min(self.W, self.H)
        out = []
        for _ in range(self.style["sparkle"]["count"]):
            b, life = rng.uniform(0, 0.65), rng.uniform(0.25, 0.45)
            x, y = rng.uniform(0.04, 0.96) * self.W, rng.uniform(0.06, 0.94) * self.H
            r, rot, c = rng.uniform(0.012, 0.042) * s, rng.uniform(0, math.pi / 2), rng.choice(self._sparkle_colors())
            local = (u - b) / life
            if 0 < local < 1:
                out.append(self._star(x, y, r, rot, c, local))
        return out

    def _sweep_geom(self):
        sp = self.style["sparkle"]
        s = min(self.W, self.H)
        if sp.get("image"):
            iw, ih = image_size(sp["image"])
            h = sp["image_size"] * s
            return h * iw / ih, h
        return 0.25 * s, 0.25 * s  # no sprite: the wipe and the trail still run

    def _sweep_pos(self, u):
        """The sprite's left x and centre y at u (0..1): enters left, leaves right, bobbing."""
        w, h = self._sweep_geom()
        e = 0.85 * u + 0.15 * ease(u)
        x = -w + (self.W + w) * e
        y = self.H * 0.6 + math.sin(u * 2 * math.pi * 4) * 0.02 * min(self.W, self.H)
        return x, y

    def sweep_sprite(self, u):
        """([sprite layer], reveal edge x): the new scene shows left of the sprite's middle."""
        sp = self.style["sparkle"]
        w, h = self._sweep_geom()
        x, y = self._sweep_pos(max(0.0, min(1.0, u)))
        edge = x + w * 0.5
        if not sp.get("image"):
            return [], edge
        self.images[sp["image"]] = {"file": sp["image"], "size": list(image_size(sp["image"]))}
        return [{"type": "image", "img": sp["image"], "dst": rr((x, y - h / 2, w, h)), "opacity": 1.0,
                 "pixelated": sp["pixelated"], "flip": sp["flip"]}], edge

    def sparkle_trail(self, seed, u):
        """Stars shed behind the sprite as it crosses."""
        import random
        rng = random.Random(seed * 104729 + 3)
        w, h = self._sweep_geom()
        s = min(self.W, self.H)
        out = []
        n = self.style["sparkle"]["count"]
        for i in range(n):
            e = i / n
            x0, y0 = self._sweep_pos(e)
            x = x0 + w * rng.uniform(0.0, 0.45)
            y = y0 + h * rng.uniform(-0.35, 0.35)
            life = rng.uniform(0.18, 0.35)
            r, rot, c = rng.uniform(0.01, 0.035) * s, rng.uniform(0, math.pi / 2), rng.choice(self._sparkle_colors())
            local = (u - e) / life
            if 0 < local < 1:
                out.append(self._star(x, y, r, rot, c, local))
        return out

    # -- logo + title cards ----------------------------------------------
    def logo_layers(self):
        lg = self.style.get("logo")
        if not lg:
            return []
        iw, ih = image_size(lg["file"])
        w = lg["size"] * self.W
        h = w * ih / iw
        m = lg["margin"] * min(self.W, self.H)
        cx, cy = corner_center(lg["corner"], 0, 0, self.W, self.H) if isinstance(lg["corner"], list) else (None, None)
        if cx is None:
            x = m if "left" in lg["corner"] else self.W - m - w
            y = m if lg["corner"].startswith("top") else self.H - m - h
        else:
            x, y = cx - w / 2, cy - h / 2
        self.images[lg["file"]] = {"file": lg["file"], "size": [iw, ih]}
        return [{"type": "image", "img": lg["file"], "dst": rr((x, y, w, h)), "opacity": lg["opacity"]}]

    def card_layers(self, spec, t):
        """A title card: background, title, an accent bar, subtitle - fading in and out (fade 0 =
        fully drawn from/to the edge: the intro's frame 0 is the thumbnail)."""
        cs, cap = self.style["card"], self.style["captions"]
        font = cs["font"] or cap["font"]
        d = spec["duration"]
        fi, fo = spec.get("fade_in", 0.4), spec.get("fade_out", 0.4)
        op = round(max(0.0, min(1.0, t / fi if fi else 1.0, (d - t) / fo if fo else 1.0)), 3)
        s = min(self.W, self.H)
        base = {"type": "text", "x": r1(self.W / 2), "bg": cs["background"], "bg_opacity": 0.0, "pad": 0.0,
                "radius": 0.0, "font": font, "hi": self.style["accent"], "opacity": op}
        icon = []
        if cs.get("image"):  # the brand icon, centred above the title
            iw, ih = image_size(cs["image"])
            h = cs["image_size"] * s
            w = h * iw / ih
            y = self.H * 0.45 - 0.07 * s - h  # its bottom a little above the title
            self.images[cs["image"]] = {"file": cs["image"], "size": [iw, ih]}
            icon = [{"type": "image", "img": cs["image"], "dst": rr((self.W / 2 - w / 2, y, w, h)), "opacity": op}]
        out = [{"type": "fill", "dst": [0, 0, self.W, self.H], "color": cs["background"]}] + icon + [
               dict(base, y=r1(self.H * 0.45), size=r1(0.075 * s), weight=700, color=cs["color"],
                    words=[{"w": spec["title"], "hi": False}]),
               {"type": "fill", "dst": rr((self.W / 2 - 0.04 * self.W, self.H * 0.45 + 0.065 * s, 0.08 * self.W, 6 * self.unit)),
                "color": self.style["accent"], "opacity": op}]
        if spec.get("subtitle"):
            out.append(dict(base, y=r1(self.H * 0.45 + 0.13 * s), size=r1(0.035 * s), weight=cap["weight"],
                            color=cs["color"], words=[{"w": spec["subtitle"], "hi": False}]))
        return out

    def warn(self, scene, msg):
        w = f"[{self.out['name']}] scenes[{scene['index']}]: {msg}"
        if w not in self.warnings:
            self.warnings.append(w)

    def scene_at(self, source, t):
        cands = [s for s in self.scenes if s["source"] == source]
        i = bisect.bisect_right([s["at"] for s in cands], t) - 1
        return cands[max(0, i)]

    def panes_for(self, scene):
        shot = scene["shot"]
        if shot["kind"] != "split":
            return [(shot, (0.0, 0.0, float(self.W), float(self.H)))]
        layout = shot["layout"]
        if layout == "auto":
            layout = "row" if self.W >= self.H else "column"
        rects = pane_rects(layout, shot["ratio"], self.W, self.H, self.style["gap"] * min(self.W, self.H))
        return list(zip(shot["panes"], rects))

    PUSH_KINDS = ("camera", "window", "screen", "rect")  # cursor/auto already move

    def pushed(self, scene, kind, view):
        """The view, slowly tightened while the scene holds (`push`), on the output clock - cuts
        inside the scene don't make it jump. Shrinks about the view's centre (the face, for the
        camera), so it stays inside the source."""
        pu = scene.get("push")
        if not pu:
            return view
        if kind not in self.PUSH_KINDS:
            self.warn(scene, f"push: only on camera, window, screen and rect shots (not {kind}); ignored")
            return view
        to = pu["to"] if pu["to"] is not None else (0.8 if self.W >= self.H else 0.9)
        elapsed = self.clock - self.scene_t0.get(scene["index"], self.clock)
        return shrink_rect(view, 1 - (1 - to) * push_amount(elapsed, pu["over"]))

    def punched(self, scene, kind, view, t, bounds):
        """The view zoomed by the scene's punch-ins. Each starts when the source reaches its
        `at` minus the lead, then runs on the output clock (a cut inside doesn't jump it); one
        whose start was cut away still fires if the source is inside it."""
        if not scene.get("punch"):
            return view
        if kind not in self.PUSH_KINDS:
            self.warn(scene, f"punch: only on camera, window, screen and rect shots (not {kind}); ignored")
            return view
        lead = PUNCH_DIP + PUNCH_IN
        s = 1.0
        for i, p in enumerate(scene["punch"]):
            key = (scene["index"], i)
            if key not in self.punch_t0 and p["at"] - lead <= t < p["at"] + p["hold"]:
                self.punch_t0[key] = self.clock
            if key in self.punch_t0:
                s *= 1 - p["zoom"] * punch_amount(self.clock - self.punch_t0[key], p["hold"], p["dip"] / p["zoom"])
        return grow_rect(view, s, *bounds) if s != 1.0 else view

    def pane_opts(self, scene, shot):
        """(fit, background, inset) for one pane: the pane's own (in a split) or the scene's."""
        return (shot.get("fit") or scene["fit"], shot.get("background") or scene["background"],
                shot.get("inset") or scene["inset"])

    def frame_aspect(self, scene, shot, dst):
        """The aspect a pane's shot is framed for: the pane's, or for an inset card with a set
        aspect that one (and its padded room's otherwise)."""
        fit, _, ins = self.pane_opts(scene, shot)
        if fit != "inset":
            return dst[2] / dst[3]
        if ins["aspect"] != "auto":
            return ins["aspect"]
        room = inset_room(dst, ins["padding"])
        return room[2] / room[3]

    def framed_shot(self, scene, shot):
        """In an inset card a window *is* the card: no margin of desktop around it (the
        default window padding), unless the edit sets one."""
        if shot["kind"] == "window" and "padding" not in shot and self.pane_opts(scene, shot)[0] == "inset":
            return dict(shot, padding=0.0)
        return shot

    def evals_for(self, scene):
        key = scene["index"]
        if key not in self.evals:
            sess = self.sessions[scene["source"]]
            self.evals[key] = [ShotEval(self.framed_shot(scene, shot), sess, scene["at"],
                                        self.frame_aspect(scene, shot, dst), lambda m, s=scene: self.warn(s, m))
                               for shot, dst in self.panes_for(scene)]
        return self.evals[key]

    def backdrop_layers(self, dst, shape, radius):
        """The style's backdrop (gradient or cover-cropped image) filling dst."""
        bd = self.style["backdrop"]
        if bd.get("image"):
            iw, ih = image_size(bd["image"])
            crop, _ = cover_rect((0.0, 0.0, float(iw), float(ih)), dst[2] / dst[3], iw, ih)
            self.images[bd["image"]] = {"file": bd["image"], "size": [iw, ih]}
            return [{"type": "image", "img": bd["image"], "dst": rr(dst), "rect": rr(crop), "shape": shape,
                     "radius": r1(radius), "bg": True}]
        return [{"type": "gradient", "dst": rr(dst), "colors": list(bd["gradient"]), "angle": bd["angle"],
                 "shape": shape, "radius": r1(radius), "bg": True}]

    def background_layers(self, bg, stream, t, cover, dst, shape, radius):
        """What's behind a contained view or an inset card: blur | backdrop | a colour."""
        if bg == "blur":
            return [{"type": "video", "src": stream, "t": round(t, 4), "rect": rr(cover), "dst": rr(dst),
                     "shape": shape, "radius": r1(radius), "blur": r1(self.style["blur"] * self.unit),
                     "dim": 0.35, "bg": True}]
        if bg == "backdrop":
            return self.backdrop_layers(dst, shape, radius)
        return [{"type": "fill", "dst": rr(dst), "color": bg, "shape": shape, "radius": r1(radius), "bg": True}]

    def scene_layers(self, scene, t, dt, first, views_out=None, move_from=None, move_u=None):
        """Layers for one scene at source time t."""
        sess = self.sessions[scene["source"]]
        src = scene["source"]
        layers = []
        split = scene["shot"]["kind"] == "split"
        if split:
            layers.append({"type": "fill", "dst": [0, 0, self.W, self.H], "color": self.style["background"],
                           "bg": True})
        radius = self.style["radius"] * self.unit if split else 0
        for ev, (shot, dst) in zip(self.evals_for(scene), self.panes_for(scene)):
            bw, bh = ev.bounds()
            aspect = dst[2] / dst[3]
            target = ev.target(t, dt, first)
            cover, loss = cover_rect(target, aspect, bw, bh)
            fit, bg, ins = self.pane_opts(scene, shot)
            mode = "cover" if fit == "cover" or (fit == "auto" and loss < LOSS_FOR_CONTAIN) else "contain"
            if ev.kind == "camera":
                mode = "cover"  # a face is never letterboxed
            if fit == "inset":
                mode = "inset"
            stream = f"{src}/{ev.source}"
            shape = "round" if split else "rect"
            if mode == "inset":
                # A rounded card with room around it, on the backdrop (or blur / a colour).
                room = inset_room(dst, ins["padding"])
                if ins["aspect"] != "auto" or ev.kind == "camera":
                    a = ins["aspect"] if ins["aspect"] != "auto" else room[2] / room[3]
                    _, _, cw, ch = fit_inside(a, room)
                    view, _ = cover_rect(target, a, bw, bh)       # cropped to the card's shape
                else:
                    view = clamp_rect(target, bw, bh) if target[2] <= bw and target[3] <= bh else target
                    _, _, cw, ch = fit_inside(view[2] / view[3], room)   # the card hugs the target
                view = self.pushed(scene, ev.kind, view)       # the card stays, the picture tightens
                view = self.punched(scene, ev.kind, view, t, (bw, bh))
                card = place_in(cw, ch, room, ins["position"])
                layers += self.background_layers(bg or "backdrop", stream, t, cover, dst, shape, radius)
                card_r = ins["radius"] * self.unit
                if ins["shadow"] > 0:
                    layers.append({"type": "shadow", "dst": rr(card), "radius": r1(card_r),
                                   "blur": r1(36 * self.unit), "offset": r1(10 * self.unit),
                                   "opacity": round(min(1.0, 0.6 * ins["shadow"]), 3), "bg": True})
                layers.append({"type": "video", "src": stream, "t": round(t, 4), "rect": rr(view), "dst": rr(card),
                               "shape": "round", "radius": r1(card_r)})
                if views_out is not None:
                    views_out.append((stream, view, card))
            elif mode == "cover":
                view = self.punched(scene, ev.kind, self.pushed(scene, ev.kind, cover), t, (bw, bh))
                if move_from is not None:
                    view = lerp_rect(move_from, view, move_u)
                layers.append({"type": "video", "src": stream, "t": round(t, 4), "rect": rr(view), "dst": rr(dst),
                               "shape": shape, "radius": r1(radius)})
                if views_out is not None:
                    views_out.append((stream, view, dst))
            else:
                tgt = clamp_rect(target, bw, bh) if target[2] <= bw and target[3] <= bh else target
                inner = fit_inside(tgt[2] / tgt[3], dst)
                tgt = self.punched(scene, ev.kind, self.pushed(scene, ev.kind, tgt), t, (bw, bh))
                layers += self.background_layers(bg or "blur", stream, t, cover, dst, shape, radius)
                layers.append({"type": "video", "src": stream, "t": round(t, 4), "rect": rr(tgt), "dst": rr(inner),
                               "shape": "round", "radius": r1(self.style["radius"] * self.unit)})
                if views_out is not None:
                    views_out.append((stream, tgt, inner))
        return layers

    # -- spotlight -------------------------------------------------------
    def spotlight_rect(self, scene, t):
        """The spotlit window's rect in screen px at t (with its border), None if the scene
        has no spotlight, or () if the window isn't on screen (then everything is blurred)."""
        sp = scene.get("spotlight")
        if not sp:
            return None
        tl = self.sessions[scene["source"]].tl
        sel = sp["window"]
        if sel.get("focused"):
            hit = tl.find_window({"focused": True}, t)
        else:
            key = scene["index"]
            if key not in self.spot_addr:
                found = tl.find_window(sel, max(t, scene["at"]))
                self.spot_addr[key] = found[0] if found else None
                if not found:
                    self.warn(scene, f"spotlight: no window matches {sel}; everything is blurred")
            addr = self.spot_addr[key]
            st = tl.window_state(addr, t) if addr else None
            hit = (addr, st) if st else None
        if not hit or not tl.visible(hit[1], t):
            return ()
        x, y, w, h = tl.rect_px(hit[1])
        b = 3 * tl.scale  # keep Hyprland's window border sharp too
        return (x - b, y - b, w + 2 * b, h + 2 * b)

    def apply_spotlight(self, scene, t, layers):
        """Every sharp screen view -> a blurred, dimmed copy + the spotlit window's part of it,
        sharp, clipped to the window (rounded like a Hyprland window when wholly in view)."""
        win = self.spotlight_rect(scene, t)
        if win is None:
            return layers
        sp = scene["spotlight"]
        out = []
        for l in layers:
            if l["type"] != "video" or not l["src"].endswith("/screen") or l.get("blur") or l.get("bg"):
                out.append(l)
                continue
            rx, ry, rw, rh = l["rect"]
            dx, dy, dw, dh = l["dst"]
            if win:
                ix0, iy0 = max(rx, win[0]), max(ry, win[1])
                ix1, iy1 = min(rx + rw, win[0] + win[2]), min(ry + rh, win[1] + win[3])
            if win and ix0 <= rx + 0.5 and iy0 <= ry + 0.5 and ix1 >= rx + rw - 0.5 and iy1 >= ry + rh - 0.5:
                out.append(l)  # zoomed inside the window: nothing to blur
                continue
            out.append(dict(l, blur=r1(sp["blur"] * self.unit), dim=sp["dim"]))
            if win and ix1 > ix0 and iy1 > iy0:
                kx, ky = dw / rw, dh / rh
                inside = ix0 > rx and iy0 > ry and ix1 < rx + rw and iy1 < ry + rh
                radius = self.style["radius"] * self.sessions[scene["source"]].tl.scale * kx if inside else 0
                out.append(dict(l, rect=rr((ix0, iy0, ix1 - ix0, iy1 - iy0)),
                                dst=rr((dx + (ix0 - rx) * kx, dy + (iy0 - ry) * ky, (ix1 - ix0) * kx, (iy1 - iy0) * ky)),
                                shape="round" if inside or l.get("shape") == "round" else "rect",
                                radius=r1(radius if inside else l.get("radius", 0))))
        return out

    def foreground_view(self, scene, layers):
        """The one sharp view of a single-pane scene (cover: the only layer; contain: the
        inner one, not the blurred background) - what a `move` glides from/to."""
        if scene["shot"]["kind"] == "split":
            return None
        vids = [l for l in layers if l["type"] == "video" and not l.get("blur") and not l.get("bg")]
        return dict(vids[-1]) if len(vids) == 1 else None

    # -- bubble ----------------------------------------------------------
    def bubble_target(self, scene):
        b = scene["bubble"]
        sess = self.sessions[scene["source"]]
        shows_camera = any(sh["kind"] == "camera" for sh, _ in self.panes_for(scene))
        show = b["show"] and sess.camera is not None and not shows_camera
        d = b["size"] * min(self.W, self.H)
        m = b["margin"] * min(self.W, self.H)
        cx, cy = corner_center(b["corner"], d, m, self.W, self.H)
        return {"cx": cx, "cy": cy, "d": d, "a": 1.0 if show else 0.0, "shape": b["shape"]}

    def bubble_layer(self, scene, t, dt, changed, trans_d):
        target = self.bubble_target(scene)
        if self.bubble_cur is None:
            self.bubble_cur = dict(target)
            self.bubble_from, self.bubble_t0 = dict(target), self.clock
        if changed:
            self.bubble_from, self.bubble_t0 = dict(self.bubble_cur), self.clock
            self.bubble_dur = trans_d if trans_d > 0 else BUBBLE_MOVE_S
        u = ease((self.clock - self.bubble_t0) / self.bubble_dur) if getattr(self, "bubble_dur", 0) else 1.0
        cur = {k: self.bubble_from[k] + (target[k] - self.bubble_from[k]) * u for k in ("cx", "cy", "d", "a")}
        cur["shape"] = target["shape"]
        self.bubble_cur = cur
        if cur["a"] <= 0.01:
            return []
        sess = self.sessions[scene["source"]]
        cw, ch = sess.cam_size
        f = sess.face_at(t)
        if f:
            fcx, fcy, fh = f
            side = min(1.0, max(0.55, fh * 1.7)) * ch
            sq = clamp_rect((fcx * cw - side / 2, fcy * ch - side / 2, side, side), cw, ch)
        else:
            sq = ((cw - ch) / 2, 0.0, float(ch), float(ch))
        if self.bubble_spring is None:
            self.bubble_spring = Spring(sq, STIFFNESS["bubble"])
        else:
            sq = clamp_rect(self.bubble_spring.step(sq, dt), cw, ch)
        d = cur["d"]
        return [{"type": "video", "src": f"{scene['source']}/camera", "t": round(t, 4), "rect": rr(sq),
                 "dst": rr((cur["cx"] - d / 2, cur["cy"] - d / 2, d, d)),
                 "shape": "circle" if cur["shape"] == "circle" else "round",
                 "radius": r1(self.style["radius"] * 3 * self.unit), "opacity": round(cur["a"], 3),
                 **({"border": [r1(max(1.0, bw * self.unit)), scene["bubble"]["border"]["color"]]}
                    if (bw := scene["bubble"]["border"]["width"]) > 0 else {})}]

    # -- rings -----------------------------------------------------------
    def ring_layers(self, scene, t, views):
        if not self.style["click_rings"]:
            return []
        tl = self.sessions[scene["source"]].tl
        out = []
        i = bisect.bisect_left([c[0] for c in tl.clicks], t - 0.5)
        for ct, px, py in tl.clicks[i:]:
            if ct > t:
                break
            age = t - ct
            for stream, view, dst in views:
                if not stream.endswith("/screen"):
                    continue
                vx, vy, vw, vh = view
                if not (vx <= px <= vx + vw and vy <= py <= vy + vh):
                    continue
                ox = dst[0] + (px - vx) / vw * dst[2]
                oy = dst[1] + (py - vy) / vh * dst[3]
                out.append({"type": "ring", "x": r1(ox), "y": r1(oy), "r": r1((10 + 40 * age / 0.5) * self.unit),
                            "width": r1(4 * self.unit), "color": self.style["accent"], "opacity": round(1 - age / 0.5, 3)})
        return out

    # -- frames ----------------------------------------------------------
    def frame(self, source, t, dt, jumped):
        self.frame_i += 1
        self.clock = self.frame_i * dt
        scene = self.scene_at(source, t)
        changed = scene is not self.cur_scene
        trans = scene["transition"]
        if changed:
            prev = self.cur_scene
            self.cur_scene = scene
            if prev is not None and not jumped and trans["type"] != "cut":
                self.prev = prev
                self.trans_start = self.clock
                self.trans = dict(trans)
                if trans["type"] == "move":
                    if self.prev_view is None or self._move_incompatible(prev, scene):
                        self.warn(scene, "move needs one view of the same source before and after "
                                         "(not a split, not screen <-> camera); using fade")
                        self.trans["type"] = "fade"
                    else:
                        self.move_from = dict(self.prev_view)
            else:
                self.prev = None
            self.evals.pop(scene["index"], None)  # fresh state when a scene (re)starts
            self.scene_t0[scene["index"]] = self.clock
            for k in [k for k in self.punch_t0 if k[0] == scene["index"]]:
                del self.punch_t0[k]   # a scene shown again punches again
        views = []
        layers = []
        # A transition lasts the frames that start before its end: round() keeps float noise from
        # adding or dropping its last frame.
        in_trans = self.prev is not None and \
            round((self.clock - self.trans_start) / dt) < round(self.trans["duration"] / dt)
        u = ease((self.clock - self.trans_start) / self.trans["duration"]) if in_trans else 1.0
        if in_trans and self.trans["type"] in ("fade", "sparkle"):
            layers += self.scene_layers(self.prev, t, dt, False)
            new = self.scene_layers(scene, t, dt, changed, views)
            for l in new:
                l["opacity"] = round(l.get("opacity", 1.0) * u, 3)
            layers += new
            if self.trans["type"] == "sparkle":
                raw = (self.clock - self.trans_start) / self.trans["duration"]
                layers += self.sparkle_burst(scene["index"], raw)
        elif in_trans and self.trans["type"] == "sweep":
            # The sprite crosses the frame left to right; the new scene is revealed behind it.
            raw = (self.clock - self.trans_start) / self.trans["duration"]
            layers += self.scene_layers(self.prev, t, dt, False)
            new = self.scene_layers(scene, t, dt, changed, views)
            sprite, edge = self.sweep_sprite(raw)
            if edge > 0:
                layers += [dict(l, clip=[0.0, 0.0, r1(min(edge, self.W)), float(self.H)]) for l in new]
            layers += self.sparkle_trail(scene["index"], raw) + sprite
        elif in_trans and self.trans["type"] == "move":
            # The sharp view glides (source rect and output rect, so contain <-> cover <-> inset
            # works); every background layer (blur, colour, backdrop, shadow) cross-fades under it.
            new = self.scene_layers(scene, t, dt, changed)
            fg = self.foreground_view(scene, new)
            for l in self.scene_layers(self.prev, t, dt, False):
                if l.get("bg"):
                    layers.append(dict(l, opacity=round(l.get("opacity", 1.0) * (1 - u), 3)))
            for l in new:
                if l.get("bg"):
                    layers.append(dict(l, opacity=round(l.get("opacity", 1.0) * u, 3)))
            a = self.move_from
            rect = lerp_rect(a["rect"], fg["rect"], u)
            dst = lerp_rect(a["dst"], fg["dst"], u)
            radius = a.get("radius", 0) + (fg.get("radius", 0) - a.get("radius", 0)) * u
            layers.append(dict(fg, rect=rr(rect), dst=rr(dst), radius=r1(radius)))
            views.append((fg["src"], rect, dst))
        else:
            self.prev = None
            layers += self.scene_layers(scene, t, dt, changed or jumped, views)
        self.prev_view = self.foreground_view(scene, layers)
        layers = self.apply_spotlight(scene, t, layers)  # after framing/transitions: they never see it
        layers += self.ring_layers(scene, t, views)
        if self.sessions[source].camera is not None:
            layers += self.bubble_layer(scene, t, dt, changed, trans["duration"] if not jumped else 0)
        layers += self.logo_layers()
        layers += self.caption_layers(source, t)
        return scene, layers

    prev_view = None

    def _move_incompatible(self, a, b):
        if a["shot"]["kind"] == "split" or b["shot"]["kind"] == "split":
            return True
        sa = "camera" if a["shot"]["kind"] == "camera" else "screen"
        sb = "camera" if b["shot"]["kind"] == "camera" else "screen"
        return sa != sb or a["source"] != b["source"]


def segments(sc, sessions):
    """[(source, from, to, out_start)] - the output timeline."""
    main = next(iter(sc["sources"]))
    if sc["timeline"]:
        segs = [(s["source"], s["from"] if s["from"] is not None else sessions[s["source"]].start,
                 s["to"] if s["to"] is not None else sessions[s["source"]].duration)
                for s in sc["timeline"]]
    else:
        dur = sessions[main].duration
        cuts = list(sc["cuts"])
        auto = sc.get("auto_cuts")
        if auto and sessions[main].transcript:
            cuts += [tuple(c) for c in transcript_mod.suggest_cuts(sessions[main].transcript, auto["pauses"],
                                                                   fillers=auto["fillers"])]
        segs, pos = [], sessions[main].start  # the edit begins after the countdown
        for a, b in sorted(cuts):
            if a > pos:
                segs.append((main, pos, min(a, dur)))
            pos = max(pos, b)
        if pos < dur:
            segs.append((main, pos, dur))
    # Title cards are segments without a source ("card:intro"/"card:outro", times = card time).
    if sc.get("intro"):
        segs.insert(0, ("card:intro", 0.0, sc["intro"]["duration"]))
    if sc.get("outro"):
        segs.append(("card:outro", 0.0, sc["outro"]["duration"]))
    out, t = [], 0.0
    for src, a, b in segs:
        if b > a:
            out.append((src, a, b, t))
            t += b - a
    return out


def stream_end_warnings(segs, sessions, sc, slack=0.5):
    """Warn when the edit uses source time after a stream's video really ends (a stalled
    encoder leaves sound with no picture; the container duration doesn't show it)."""
    out = []
    uses_camera = {s["source"] for s in sc["scenes"]
                   if s["shot"]["kind"] == "camera" or any(p["kind"] == "camera" for p in s["shot"].get("panes", []))
                   or s["bubble_raw"] is not False}
    for name, sess in sessions.items():
        used = max((b for src, a, b, o in segs if src == name), default=None)
        if used is None:
            continue
        for kind in ("screen", "camera"):
            if kind == "camera" and (not sess.camera or name not in uses_camera):
                continue
            end = sess.video_end(kind)
            if end is not None and used > end + slack:
                out.append(f"source {name}: {kind} video ends at {end:.1f}s but the edit runs to {used:.1f}s "
                           f"(no {kind} picture after {end:.1f}s - a stalled recording? trim or re-record)")
    return out


def load_sessions(sc):
    return {name: Session(s["path"]) for name, s in sc["sources"].items()}


def resolve(sc, output_name=None, sessions=None, fps=None, t_from=0.0, t_to=None):
    """The plan for one output (dict, JSON-ready)."""
    sessions = sessions or load_sessions(sc)
    out = next(o for o in sc["outputs"] if output_name in (None, o["name"]))
    fps = fps or out["fps"]
    warnings = []
    res = OutputResolver(sc, out, sessions, warnings)
    segs = segments(sc, sessions)
    warnings.extend(stream_end_warnings(segs, sessions, sc))
    total = segs[-1][3] + (segs[-1][2] - segs[-1][1]) if segs else 0.0
    t_to = total if t_to is None else min(t_to, total)
    n = max(0, int(round((t_to - t_from) * fps)))
    frames = []
    dt = 1 / fps
    seg_i = 0
    last_src = None
    scene_first = {}
    for i in range(n):
        t_out = t_from + i * dt
        while seg_i + 1 < len(segs) and t_out >= segs[seg_i + 1][3]:
            seg_i += 1
        src, a, b, o = segs[seg_i]
        t_src = a + (t_out - o)
        jumped = last_src is not None and (src != last_src[0] or abs(t_src - last_src[1] - dt) > 1e-6)
        last_src = (src, t_src)
        if src.startswith("card:"):
            spec = sc[src[5:]]
            frames.append({"t": round(t_out, 4), "src": src, "st": round(t_src, 4), "scene": -1,
                           "layers": res.card_layers(spec, t_src) + res.logo_layers()})
            continue
        scene, layers = res.frame(src, t_src, dt, jumped)
        scene_first.setdefault(scene["index"], round(t_out, 3))
        frames.append({"t": round(t_out, 4), "src": src, "st": round(t_src, 4), "scene": scene["index"],
                       "layers": layers})

    streams = {}
    for name, sess in sessions.items():
        streams[f"{name}/screen"] = {"file": os.path.join(sess.path, sess.screen["file"]),
                                     "size": [sess.tl.W, sess.tl.H], "offset": 0.0}
        if sess.camera:
            streams[f"{name}/camera"] = {"file": os.path.join(sess.path, sess.camera["file"]),
                                         "size": list(sess.cam_size), "offset": sess.camera["first_frame_t"] / 1e9}
    audio = []
    if sc["audio"]["source"] != "none":
        for name, sess in sessions.items():
            clean = os.path.join(sess.path, "mic-clean.flac")
            if sc["audio"]["source"] == "mic-clean" and os.path.exists(clean):
                audio.append({"source": name, "file": clean})
            elif sess.screen.get("audio"):
                audio.append({"source": name, "file": os.path.join(sess.path, sess.screen["file"])})
    return {
        "version": 1,
        "output": {"name": out["name"], "size": list(out["size"]), "fps": fps},
        "duration": round(total, 4),
        "streams": streams,
        "images": res.images,
        "audio": audio,
        "segments": [{"source": s, "from": round(a, 4), "to": round(b, 4), "out": round(o, 4)} for s, a, b, o in segs],
        "scenes": [{"index": s["index"], "label": s.get("label") or describe(s["shot"]), "at": s["at"],
                    "source": s["source"], "out": scene_first.get(s["index"])} for s in sc["scenes"]],
        "warnings": warnings,
        "frames": frames,
    }


def describe(shot):
    k = shot["kind"]
    if k == "window":
        sel = shot["select"]
        what = "focused window" if sel.get("focused") else " ".join(f"{a}={b}" for a, b in sel.items())
        return f"window {what}" + (" (region)" if "region" in shot else "") + (" follow" if shot.get("follow") else "")
    if k == "split":
        return "split: " + " | ".join(describe(p) for p in shot["panes"])
    if k == "cursor":
        return f"cursor x{shot['zoom']:g}"
    return k
