"""GPU build compositor (pugrec-gpu) against compose.py, the reference: the same plan painted
both ways, compared as raw NV12 (no encoder in between). Skipped without the binary
(scripts/build-gpu.sh) or a GPU.

    .venv/bin/python -m unittest tests.test_gpu -v
"""

import os
import subprocess
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pugrec.edit import compose as C  # noqa: E402
from pugrec.edit import gpu as G  # noqa: E402

W, H, FPS = 320, 180, 30


def to_nv12(bgr):
    """The CPU path's colour conversion (ffmpeg's default bgr24 -> yuv, as the x264 encoder gets it)."""
    h, w = bgr.shape[:2]
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-i", "-",
                          "-pix_fmt", "nv12", "-f", "rawvideo", "-"], input=bgr.tobytes(), capture_output=True).stdout
    return out


def cpu_frames(plan):
    """compose.py's frames (bgr), decoded and painted as _render_range does."""
    W_, H_ = plan["output"]["size"]
    fps = plan["output"]["fps"]
    canvas = np.zeros((H_, W_, 3), np.uint8)
    frames = plan["frames"]
    out = []
    for r0, r1, _src, st in C.runs(frames, fps):
        run = frames[r0:r1]
        used = {l["src"] for f in run for l in f["layers"] if l["type"] == "video"}
        held = bool(run[0].get("hold"))
        decs = {s: C.Decoder(plan["streams"][s], st, fps, 1 if held else len(run)) for s in used}
        still = {s: d.next() for s, d in decs.items()} if held else None
        for f in run:
            C.paint(canvas, f["layers"], still or {s: d.next() for s, d in decs.items()}, plan.get("images"))
            out.append(canvas.copy())
        for d in decs.values():
            d.close()
    return out


def gpu_frames(plan, tmp):
    """pugrec-gpu's NV12 frames (bytes) for the whole plan."""
    W_, H_ = plan["output"]["size"]
    job = G.Job(plan, tmp)
    p = subprocess.Popen([G.binary()], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def feed():
        for line in job.lines(plan["frames"]):
            p.stdin.write(line.encode() + b"\n")
        p.stdin.close()

    t = threading.Thread(target=feed)
    t.start()
    data = p.stdout.read()
    t.join()
    p.wait()
    n = W_ * H_ * 3 // 2
    return [data[i:i + n] for i in range(0, len(data), n)]


def planes(nv12, w, h):
    a = np.frombuffer(nv12, np.uint8)
    return a[:w * h].reshape(h, w).astype(np.int16), a[w * h:].reshape(h // 2, w).astype(np.int16)


def card_plan(layers_per_frame, w=W, h=H):
    return {"output": {"name": "t", "size": [w, h], "fps": FPS}, "streams": {}, "images": {}, "audio": [],
            "frames": [{"t": i / FPS, "src": "card:intro", "st": i / FPS, "scene": -1, "layers": ls}
                       for i, ls in enumerate(layers_per_frame)]}


class Compare:
    def compare(self, plan, max_y=4, mean_y=1.0, max_uv=4, mean_uv=1.0, label=""):
        with tempfile.TemporaryDirectory() as tmp:
            g = gpu_frames(plan, tmp)
        c = cpu_frames(plan)
        w, h = plan["output"]["size"]
        self.assertEqual(len(g), len(c), "frame count")
        stats = []
        for i, (gf, cf) in enumerate(zip(g, c)):
            gy, guv = planes(gf, w, h)
            cy, cuv = planes(to_nv12(cf), w, h)
            dy, duv = np.abs(gy - cy), np.abs(guv - cuv)
            stats.append((dy.max(), dy.mean(), duv.max(), duv.mean()))
            dbg = os.environ.get("PUGREC_GPU_DEBUG")
            if dbg:   # CPU over GPU over |diff| x8, as PNG
                import cv2
                os.makedirs(dbg, exist_ok=True)
                g_bgr = cv2.cvtColor(np.frombuffer(gf, np.uint8).reshape(h * 3 // 2, w), cv2.COLOR_YUV2BGR_NV12)
                c_bgr = cv2.cvtColor(np.frombuffer(to_nv12(cf), np.uint8).reshape(h * 3 // 2, w), cv2.COLOR_YUV2BGR_NV12)
                d = np.clip(np.abs(g_bgr.astype(int) - c_bgr.astype(int)) * 8, 0, 255).astype(np.uint8)
                cv2.imwrite(os.path.join(dbg, f"{label.replace('/', '-')}-{i}.png"), np.concatenate([c_bgr, g_bgr, d], 0))
        my, ay, muv, auv = (max(s[k] for s in stats) for k in range(4))
        msg = f"{label}: Y max {my} mean {ay:.2f}, UV max {muv} mean {auv:.2f}"
        self.assertLessEqual(my, max_y, msg)
        self.assertLessEqual(ay, mean_y, msg)
        self.assertLessEqual(muv, max_uv, msg)
        self.assertLessEqual(auv, mean_uv, msg)
        return msg


@unittest.skipUnless(G.binary(), "pugrec-gpu not built (scripts/build-gpu.sh)")
class Parity(Compare, unittest.TestCase):
    def test_fill_shapes_and_opacity(self):
        frames = []
        for shape, radius in (("rect", 0), ("round", 12.4), ("circle", 0), ("rect", 7)):
            frames.append([
                {"type": "fill", "dst": [0, 0, W, H], "color": "#203040"},
                {"type": "fill", "dst": [20.4, 30.6, 151.3, 90.5], "color": "#e8a020", "shape": shape, "radius": radius},
                {"type": "fill", "dst": [120, 60, 150, 100], "color": "#10c0ff", "shape": shape, "radius": radius,
                 "opacity": 0.55},
            ])
        print(self.compare(card_plan(frames), label="fill"))

    def test_gradient(self):
        frames = [[{"type": "gradient", "dst": [0, 0, W, H], "colors": cs, "angle": a, "shape": "round", "radius": r}]
                  for cs, a, r in ((["#ff0000", "#0000ff"], 90, 0), (["#101820", "#f0e0c0", "#204060"], 160, 20),
                                   (["#000000", "#ffffff", "#ff00ff", "#00ff00"], 33, 0))]
        print(self.compare(card_plan(frames), label="gradient"))

    def test_shadow(self):
        frames = [[{"type": "fill", "dst": [0, 0, W, H], "color": "#e0e0e0"},
                   {"type": "shadow", "dst": [60, 40, 200, 100], "radius": r, "blur": b, "offset": o, "opacity": op}]
                  for r, b, o, op in ((10, 20, 4, 0.3), (0, 36, 10, 0.6), (24, 8, 0, 1.0))]
        print(self.compare(card_plan(frames), label="shadow"))

    def test_ring_star_stroke(self):
        pts = [[40, 40], [200, 40.5], [260.3, 120], [80, 150], [30.7, 90]]
        frames = [[{"type": "fill", "dst": [0, 0, W, H], "color": "#101010"},
                   {"type": "ring", "x": 160.6, "y": 90.2, "r": r, "width": wd, "color": "#ffcc00", "opacity": op},
                   {"type": "star", "x": 70.3, "y": 60.8, "r": r / 2 + 3, "rot": r / 10, "color": "#ffffff", "opacity": op},
                   {"type": "stroke", "points": pts, "closed": closed, "width": wd, "color": "#40e0ff", "opacity": op}]
                  for r, wd, op, closed in ((20, 4, 1.0, True), (45.5, 2.2, 0.6, False), (8, 1, 0.9, True))]
        print(self.compare(card_plan(frames), label="ring/star/stroke"))

    def test_text_and_image_sprites(self):
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image
            png = os.path.join(tmp, "logo.png")
            im = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
            for x in range(64):
                for y in range(48):
                    im.putpixel((x, y), (x * 4, y * 5, 200, 255 if (x - 32) ** 2 + (y - 24) ** 2 < 400 else 60))
            im.save(png)
            text = {"type": "text", "x": 160, "y": 140, "size": 22, "words": [{"w": "Hello", "hi": False},
                    {"w": "Acme", "hi": True}], "color": "#ffffff", "hi": "#ffcc00", "bg": "#000000",
                    "bg_opacity": 0.78, "font": "IBM Plex Sans", "weight": 600, "pad": 10, "radius": 8}
            frames = [[{"type": "fill", "dst": [0, 0, W, H], "color": "#336699"},
                       {"type": "image", "img": png, "dst": [10.4, 10.6, 96, 72], "shape": "round", "radius": 10},
                       {"type": "image", "img": png, "dst": [150, 10, 64, 48], "pixelated": True, "flip": True,
                        "opacity": 0.7},
                       dict(text, opacity=op), dict(text, y=60, x=180, align="left", bg_opacity=0.0)]
                      for op in (1.0, 0.5)]
            plan = card_plan(frames)
            plan["images"] = {png: {"file": png, "size": [64, 48]}}
            print(self.compare(plan, label="sprites"))

    def test_clip(self):
        frames = [[{"type": "fill", "dst": [0, 0, W, H], "color": "#000000"},
                   {"type": "fill", "dst": [10, 10, 300, 160], "color": "#ff8800", "clip": [0, 0, edge, H]},
                   {"type": "ring", "x": 100, "y": 90, "r": 30, "width": 4, "color": "#ffffff", "clip": [0, 0, edge, H]}]
                  for edge in (40.5, 101.5, 250)]
        print(self.compare(card_plan(frames), label="clip"))


def make_clip(path, w, h, fps, secs):
    """A moving test pattern (every frame differs), AV1 full range like the recorder's."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={secs}",
                    "-c:v", "libsvtav1", "-preset", "12", "-crf", "30", "-pix_fmt", "yuv420p", "-color_range", "pc",
                    path], check=True)


@unittest.skipUnless(G.binary(), "pugrec-gpu not built (scripts/build-gpu.sh)")
class VideoParity(Compare, unittest.TestCase):
    """Frame choice and video layers: two streams (one with an offset and its own fps), a cut,
    a hold and a run that starts before the camera does."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.screen = os.path.join(cls.tmp.name, "screen.mkv")
        cls.camera = os.path.join(cls.tmp.name, "camera.mkv")
        make_clip(cls.screen, 1280, 720, 60, 6)
        make_clip(cls.camera, 640, 360, 30, 6)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plan(self, layers):
        streams = {"s/screen": {"file": self.screen, "size": [1280, 720], "offset": 0.0},
                   "s/camera": {"file": self.camera, "size": [640, 360], "offset": 0.37}}
        frames, t = [], 0.0
        for st0, n, hold in ((0.1, 12, False), (2.5, 20, False), (3.0, 8, True), (3.0, 10, False), (5.7, 15, False)):
            for i in range(n):
                f = {"t": t, "src": "s", "st": st0 if hold else st0 + i / FPS, "scene": 0, "layers": layers}
                if hold:
                    f["hold"] = True
                frames.append(f)
                t += 1 / FPS
        return {"output": {"name": "t", "size": [W, H], "fps": FPS}, "streams": streams, "images": {}, "audio": [],
                "frames": frames}

    def test_frame_choice_cut_hold(self):
        layers = [{"type": "video", "src": "s/screen", "rect": [0, 0, 1280, 720], "dst": [0, 0, W, H]},
                  {"type": "video", "src": "s/camera", "rect": [120.3, 0, 360, 360], "dst": [10.4, 90.2, 80, 80],
                   "shape": "circle", "border": [3, "#f1e4d9"]}]
        print(self.compare(self.plan(layers), label="video"))

    def test_zoom_blur_dim_round(self):
        layers = [{"type": "video", "src": "s/screen", "rect": [-40, -20, 1360, 760], "dst": [0, 0, W, H],
                   "blur": 40, "dim": 0.35},
                  {"type": "video", "src": "s/screen", "rect": [301.7, 155.2, 210.5, 118.4], "dst": [40.5, 20.3, 240, 135],
                   "shape": "round", "radius": 9.5, "opacity": 0.8}]
        print(self.compare(self.plan(layers), label="video zoom/blur"))


if __name__ == "__main__":
    unittest.main()
