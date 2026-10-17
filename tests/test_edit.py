"""Editor: script validation and the resolver (no video).  .venv/bin/python -m unittest discover tests"""

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_session  # noqa: E402
from pugrec.edit import resolve as R  # noqa: E402
from pugrec.edit import script as S  # noqa: E402

SEC = 1_000_000_000


def make_session(d, camera=True):
    """fake_session's 10 s timeline + the right window moving at 3 s + a camera stream."""
    recs = list(fake_session.records())
    # At 3 s the right window becomes small and moves to the lower right (floating, dragged).
    recs.insert(-1, {"t": 3 * SEC, "type": "window_update", "address": "0xb", "at": [3400, 1100], "size": [1200, 900]})
    if camera:
        recs.insert(2, {"t": int(0.5 * SEC), "type": "stream_start", "stream": "camera", "file": "camera.mkv",
                        "pixels": [1920, 1080], "fps": 30, "codec": "av1", "first_frame_t": int(0.5 * SEC)})
        open(os.path.join(d, "camera.mkv"), "w").close()
    with open(os.path.join(d, "session.jsonl"), "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return d


def plan(data, d, output=None, fps=10):
    sc = S.parse(data, d)
    return R.resolve(sc, output, fps=fps)


def frame_at(p, t):
    return min(p["frames"], key=lambda f: abs(f["t"] - t))


def videos(f, src=None):
    return [l for l in f["layers"] if l["type"] == "video" and (src is None or l["src"].endswith(src))]


class ScriptErrors(unittest.TestCase):
    def check(self, data, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse(data)
        self.assertIn(needle, str(cm.exception))

    def test_unknown_key_suggests(self):
        self.check({"scenes": [{"at": 0, "shot": {"window": {"clas": "kitty"}}}]},
                   "scenes[0].shot.window: unknown key 'clas' (did you mean 'class'?)")

    def test_unknown_shot(self):
        self.check({"scenes": [{"at": 0, "shot": "camra"}]}, "unknown shot 'camra' (did you mean 'camera'?)")

    def test_split_needs_two(self):
        self.check({"scenes": [{"at": 0, "shot": {"split": ["screen"]}}]}, "exactly two shots")

    def test_region_bounds(self):
        self.check({"scenes": [{"at": 0, "shot": {"window": {"class": "x"}, "region": [0.5, 0, 0.6, 1]}}]},
                   "x + w and y + h must be <= 1")

    def test_scene_order(self):
        self.check({"scenes": [{"at": 5, "shot": "screen"}, {"at": 2, "shot": "camera"}]}, "must be after")

    def test_cuts_and_timeline_exclusive(self):
        self.check({"cuts": [[1, 2]], "timeline": [{"source": "main"}], "scenes": [{"at": 0, "shot": "screen"}]},
                   "either timeline")

    def test_overlays_not_yet(self):
        self.check({"overlays": [{"source": "main"}], "scenes": [{"at": 0, "shot": "screen"}]}, "not supported yet")

    def test_output_override_key_checked(self):
        self.check({"outputs": [{"name": "vertical", "size": "1080x1920"}],
                    "scenes": [{"at": 0, "shot": "screen", "vertical": {"shoot": "camera"}}]},
                   "scenes[0].vertical: unknown key 'shoot' (did you mean 'shot'?)")


class Resolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_screen_on_16_9_is_contain_with_blur(self):
        # 5120x2160 (2.37:1) on 16:9: a cover crop loses 25% -> contain + blurred background (item 5).
        p = plan({"scenes": [{"at": 0, "shot": "screen", "bubble": False}]}, self.d)
        f = frame_at(p, 1.0)
        bg, fg = videos(f)[:2]
        self.assertGreater(bg.get("blur", 0), 0)
        self.assertEqual(fg["rect"], [0.0, 0.0, 5120.0, 2160.0])
        self.assertAlmostEqual(fg["dst"][2] / fg["dst"][3], 5120 / 2160, places=2)
        self.assertEqual(fg["dst"][2], 1920.0)

    def test_window_follow_tracks_the_move(self):
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "right"}, "follow": True}, "fit": "cover"}]},
                 self.d)
        before = videos(frame_at(p, 2.5))[0]["rect"]
        after = videos(frame_at(p, 6.0))[0]["rect"]
        self.assertLess(before[1], 100)
        self.assertGreater(after[1], before[1] + 500)   # followed the window down...
        self.assertLess(after[2], before[2] * 0.7)      # ...and zoomed in on the smaller window
        cx = after[0] + after[2] / 2
        self.assertAlmostEqual(cx, 3400 + 600, delta=60)  # centred on it

    def test_region_inside_window(self):
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "left"}, "region": [0, 0, 0.5, 0.25]},
                              "fit": "contain", "bubble": False}]}, self.d)
        fg = videos(frame_at(p, 1.0))[-1]
        x, y, w, h = fg["rect"]
        self.assertLess(w, 2540 * 0.6)                 # about half the window wide (+ padding)
        self.assertGreaterEqual(x, 0)

    def test_vertical_window_gets_blur(self):
        p = plan({"outputs": [{"name": "vertical", "size": "1080x1920"}],
                  "scenes": [{"at": 0, "shot": {"window": {"title": "right"}}, "bubble": False}]}, self.d)
        f = frame_at(p, 1.0)
        vs = videos(f)
        self.assertEqual(len(vs), 2)
        self.assertGreater(vs[0]["blur"], 0)
        self.assertEqual(vs[1]["dst"][2], 1080.0)       # the window fills the width

    def test_cuts_remove_time(self):
        p = plan({"cuts": [[2, 4]], "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertAlmostEqual(p["duration"], 8.0)
        f = frame_at(p, 2.5)                            # output 2.5 s = source 4.5 s
        self.assertAlmostEqual(f["st"], 4.5, places=3)

    def test_two_segments_same_source_splice_path(self):
        p = plan({"timeline": [{"source": "main", "from": 6, "to": 8}, {"source": "main", "from": 1, "to": 2}],
                  "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertAlmostEqual(p["duration"], 3.0)
        self.assertAlmostEqual(frame_at(p, 0.0)["st"], 6.0)
        self.assertAlmostEqual(frame_at(p, 2.5)["st"], 1.5)
        for f in p["frames"]:
            for l in f["layers"]:
                if l["type"] == "video":
                    self.assertIn("/", l["src"])        # every layer names its source stream
                    self.assertAlmostEqual(l["t"], f["st"], places=3)

    def test_split_row_and_column(self):
        data = {"outputs": [{"name": "main", "size": "1920x1080"}, {"name": "vertical", "size": "1080x1920"}],
                "scenes": [{"at": 0, "shot": {"split": [{"window": {"title": "left"}}, "camera"]}}]}
        sc = S.parse(data, self.d)
        main = R.resolve(sc, "main", fps=5)
        vert = R.resolve(sc, "vertical", fps=5)
        m = [l for l in videos(frame_at(main, 1)) if not l.get("blur")][:2]
        v = [l for l in videos(frame_at(vert, 1)) if not l.get("blur")][:2]
        self.assertLess(m[0]["dst"][0], m[1]["dst"][0])        # side by side
        self.assertLess(v[0]["dst"][1], v[1]["dst"][1])        # stacked
        self.assertEqual(frame_at(main, 1)["layers"][0]["type"], "fill")

    def test_move_transition_interpolates(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen", "fit": "cover", "bubble": False},
                             {"at": 5, "shot": {"rect": [0, 0, 0.25, 0.25]}, "fit": "cover",
                              "transition": {"move": 1.0}, "bubble": False}]}, self.d)
        w0 = videos(frame_at(p, 4.9))[0]["rect"][2]
        wm = videos(frame_at(p, 5.5))[0]["rect"][2]
        w1 = videos(frame_at(p, 6.5))[0]["rect"][2]
        self.assertGreater(w0, wm)
        self.assertGreater(wm, w1)

    def test_move_from_contain_into_a_zoom(self):
        # A window shown whole (contain + blur) gliding into a cursor zoom (cover): the sharp view
        # grows from letterboxed to full frame while the blurred background fades out.
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "left"}}, "fit": "contain", "bubble": False},
                             {"at": 5, "shot": {"cursor": {"zoom": 2}}, "transition": {"move": 1.0},
                              "bubble": False}]}, self.d)
        self.assertFalse([w for w in p["warnings"] if "move" in w])
        sharp = lambda f: [l for l in videos(f) if not l.get("blur")][-1]
        before, mid, after = (sharp(frame_at(p, t))["dst"][2] for t in (4.9, 5.5, 6.5))
        self.assertLess(before, mid)
        self.assertLess(mid, after)
        self.assertEqual(after, 1920.0)
        blurs = [l for l in videos(frame_at(p, 5.5)) if l.get("blur")]
        self.assertTrue(blurs and 0 < blurs[0]["opacity"] < 1)

    def test_a_cut_inside_a_fade_does_not_end_it_early(self):
        # The fade runs on output time: a 0.5 s cut 0.3 s into a 1 s fade used to jump it to
        # 80% done in one frame (a visible pop).
        p = plan({"cuts": {"ranges": [[5.3, 5.8]]},
                  "scenes": [{"at": 0, "shot": "screen", "fit": "cover", "bubble": False},
                             {"at": 5, "shot": {"rect": [0, 0, 0.25, 0.25]}, "fit": "cover",
                              "transition": {"fade": 1.0}, "bubble": False}]}, self.d)
        ops = []
        for f in p["frames"]:
            if 5.0 <= f["t"] <= 6.3:
                vids = videos(f)
                ops.append(vids[-1].get("opacity", 1.0) if len(vids) > 1 else 1.0)
        steps = [b - a for a, b in zip(ops, ops[1:])]
        self.assertLess(max(steps), 0.25)                     # no jump (10 fps, 1 s ease: < 0.16 a step)
        self.assertGreaterEqual(sum(1 for o in ops if o < 1.0), 9)   # ~1 s of output frames fading

    def test_move_cross_fades_a_colour_background(self):
        # contain on a colour (not blur): the colour used to vanish for the whole move.
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "left"}}, "fit": "contain",
                              "background": "#123456", "bubble": False},
                             {"at": 5, "shot": {"cursor": {"zoom": 2}}, "transition": {"move": 1.0},
                              "bubble": False}]}, self.d)
        fills = [l for l in frame_at(p, 5.5)["layers"] if l["type"] == "fill" and l["color"] == "#123456"]
        self.assertTrue(fills and 0 < fills[0]["opacity"] < 1)
        self.assertTrue(fills[0]["bg"])

    def test_spotlight_blurs_all_but_the_window(self):
        p = plan({"style": {"spotlight": {"window": {"title": "right"}}},
                  "scenes": [{"at": 0, "shot": "screen", "fit": "cover", "bubble": False}]}, self.d)
        vs = videos(frame_at(p, 1.0))
        self.assertEqual(len(vs), 2)
        self.assertGreater(vs[0]["blur"], 0)                       # the whole view, blurred + dimmed
        self.assertGreater(vs[0]["dim"], 0)
        sharp = vs[1]
        self.assertFalse(sharp.get("blur"))
        x, y, w, h = sharp["rect"]
        self.assertAlmostEqual(x, 2570 - 3, delta=1)               # the right window (+ border)
        self.assertLess(w, 2600)
        # The sharp part lands where the window is in the output.
        bx, by, bw, bh = vs[0]["rect"]
        self.assertAlmostEqual(sharp["dst"][0], (x - bx) / bw * 1920, delta=1)

    def test_spotlight_off_per_scene_and_inside_window(self):
        p = plan({"style": {"spotlight": {"window": {"title": "right"}}},
                  "scenes": [{"at": 0, "shot": "screen", "fit": "cover", "spotlight": False, "bubble": False},
                             {"at": 5, "shot": {"window": {"title": "right"}, "region": [0.2, 0.2, 0.3, 0.3]},
                              "fit": "cover", "transition": "cut", "bubble": False}]}, self.d)
        self.assertFalse([l for l in videos(frame_at(p, 1.0)) if l.get("blur") and l.get("dim")])  # off
        zoomed = videos(frame_at(p, 7.0))
        self.assertEqual(len(zoomed), 1)                           # zoomed inside the window: no blur
        self.assertFalse(zoomed[0].get("blur"))

    def test_spotlight_leaves_camera_alone(self):
        p = plan({"style": {"spotlight": True}, "scenes": [{"at": 0, "shot": "camera"}]}, self.d)
        self.assertFalse([l for l in videos(frame_at(p, 2.0)) if l.get("blur")])

    def test_camera_follow_false_holds_the_framing(self):
        # A face drifting left to right: the camera follows it by default, holds with follow: false.
        with open(os.path.join(self.d, "faces.jsonl"), "w") as f:
            for i in range(101):
                f.write(json.dumps({"t": int(i * 0.1 * SEC), "x": 0.2 + 0.4 * i / 100, "y": 0.3,
                                    "w": 0.1, "h": 0.2}) + "\n")
        try:
            moving = plan({"scenes": [{"at": 0, "shot": {"camera": {"zoom": 1.5}}}]}, self.d)
            still = plan({"scenes": [{"at": 0, "shot": {"camera": {"zoom": 1.5}, "follow": False}}]}, self.d)
        finally:
            os.remove(os.path.join(self.d, "faces.jsonl"))
        xs = lambda p: [videos(frame_at(p, t), "/camera")[0]["rect"][0] for t in (1.0, 5.0, 9.0)]
        self.assertGreater(xs(moving)[2], xs(moving)[0] + 300)    # followed the face
        self.assertEqual(len(set(xs(still))), 1)                   # held where it started
        self.assertEqual(xs(still)[0], videos(frame_at(moving, 0.0), "/camera")[0]["rect"][0])

    def test_fade_transition_blends(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen", "bubble": False},
                             {"at": 5, "shot": "camera", "transition": {"fade": 1.0}}]}, self.d)
        f = frame_at(p, 5.5)
        cam = videos(f, "/camera")
        self.assertTrue(videos(f, "/screen"))
        self.assertTrue(0 < cam[0]["opacity"] < 1)

    def test_bubble_moves_between_corners(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen"},
                             {"at": 5, "shot": "screen", "bubble": {"corner": "bottom-right"},
                              "transition": "cut"}]}, self.d)
        x0 = videos(frame_at(p, 4.9), "/camera")[0]["dst"][0]
        xm = videos(frame_at(p, 5.2), "/camera")[0]["dst"][0]
        x1 = videos(frame_at(p, 6.0), "/camera")[0]["dst"][0]
        self.assertLess(x0, xm)
        self.assertLess(xm, x1)
        self.assertGreater(x1, 1400)

    def test_bubble_settings_stick(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen", "bubble": {"corner": "top-right"}},
                             {"at": 5, "shot": {"rect": [0, 0, 0.5, 0.5]}}]}, self.d)
        b = videos(frame_at(p, 7), "/camera")[0]
        self.assertGreater(b["dst"][0], 1400)            # still on the right...
        self.assertLess(b["dst"][1], 200)                # ...and at the top

    def test_bubble_hidden_on_camera_shot(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera"}]}, self.d)
        f = frame_at(p, 2)
        self.assertEqual(len(videos(f, "/camera")), 1)          # the full camera only, no bubble

    def test_click_ring_mapped_into_view(self):
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "right"}}, "fit": "cover"}]}, self.d)
        rings = [l for l in frame_at(p, 2.1)["layers"] if l["type"] == "ring"]
        self.assertEqual(len(rings), 1)
        self.assertTrue(0 <= rings[0]["x"] <= 1920 and 0 <= rings[0]["y"] <= 1080)

    def test_warns_when_the_edit_runs_past_the_video(self):
        from unittest import mock
        # A stalled encoder: the screen video stops at 6 s of a 10 s recording.
        with mock.patch.object(R.Session, "video_end", lambda self, kind: 6.0 if kind == "screen" else None):
            p = plan({"scenes": [{"at": 0, "shot": "screen"}]}, self.d)
            self.assertTrue(any("screen video ends at 6.0s but the edit runs to 10.0s" in w for w in p["warnings"]))
            p = plan({"timeline": [{"source": "main", "from": 0, "to": 5.8}], "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
            self.assertFalse([w for w in p["warnings"] if "video ends" in w])   # trimmed before the stall: fine

    def test_missing_window_warns_and_falls_back(self):
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"class": "nope"}}}]}, self.d)
        self.assertTrue(any("no window matches" in w for w in p["warnings"]))
        self.assertTrue(videos(frame_at(p, 1)))


def add_transcript(d):
    """Words: 'So this is the demo.' 1.0-2.5 s, a 3 s pause, 'Now um the editor' 5.5-7 s."""
    words = [(1.0, 1.2, "So"), (1.25, 1.5, "this"), (1.55, 1.7, "is"), (1.75, 1.9, "the"), (1.95, 2.5, "demo."),
             (5.5, 5.8, "Now"), (5.85, 6.1, "um"), (6.15, 6.4, "the"), (6.45, 7.0, "editor")]
    seg = {"type": "segment", "t": int(1.0 * SEC), "end": int(7 * SEC), "text": "...",
           "words": [{"t": int(a * SEC), "end": int(b * SEC), "w": w, "p": 0.9} for a, b, w in words]}
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        f.write(json.dumps({"type": "meta", "t": 0}) + "\n" + json.dumps(seg) + "\n")


class Inset(unittest.TestCase):
    """fit: inset - a rounded card with room around it, on the backdrop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def split(self, pane_extra=None, **scene):
        cam = {"camera": {}, "fit": "inset", "inset": {"aspect": "2:3"}}
        cam.update(pane_extra or {})
        return {"scenes": [dict({"at": 0, "shot": {"split": [{"window": {"title": "left"}}, cam], "ratio": 0.65},
                                 "bubble": False}, **scene)]}

    def test_camera_card_in_a_split_like_the_screenshot(self):
        p = plan(self.split(), self.d)
        f = frame_at(p, 5.0)
        cam = videos(f, "/camera")[0]
        x, y, w, h = cam["dst"]
        self.assertAlmostEqual(w / h, 2 / 3, places=2)                 # a portrait card
        self.assertAlmostEqual(cam["rect"][2] / cam["rect"][3], 2 / 3, places=2)   # cropped to it
        self.assertEqual(cam["shape"], "round")
        self.assertAlmostEqual(cam["radius"], 18.0)
        grads = [l for l in f["layers"] if l["type"] == "gradient"]
        self.assertEqual(len(grads), 1)                                 # the PugPal backdrop, right pane only
        gx, gy, gw, gh = grads[0]["dst"]
        self.assertGreater(gx, 1000)
        self.assertTrue(gx < x and x + w < gx + gw and gy < y and y + h < gy + gh)   # card inside, with room
        self.assertEqual(grads[0]["colors"], ["#16171a", "#32231f", "#e8480f"])
        self.assertTrue(grads[0]["bg"])
        self.assertEqual(videos(f, "/screen")[0].get("dst")[0] < 100, True)          # the window pane untouched

    def test_whole_frame_window_card_hugs_the_window(self):
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "left"}}, "fit": "inset", "bubble": False}]}, self.d)
        f = frame_at(p, 1.0)
        card = videos(f, "/screen")[0]
        x, y, w, h = card["dst"]
        rx, ry, rw, rh = card["rect"]
        self.assertAlmostEqual(w / h, rw / rh, places=2)                # its own shape, no crop
        pad = 0.06 * 1080
        self.assertGreaterEqual(y, pad - 0.5)
        self.assertLessEqual(y + h, 1080 - pad + 0.5)
        self.assertEqual([l for l in f["layers"] if l["type"] == "gradient"][0]["dst"], [0.0, 0.0, 1920.0, 1080.0])
        # The window *is* the card: no margin of desktop around it (fake_session: left at 10,54 2540x2096).
        self.assertEqual(card["rect"], [10.0, 54.0, 2540.0, 2096.0])

    def test_pane_fit_wins_over_the_scene_fit(self):
        p = plan(self.split(fit="cover"), self.d)
        f = frame_at(p, 5.0)
        self.assertAlmostEqual(videos(f, "/camera")[0]["radius"], 18.0)   # the pane's inset
        self.assertAlmostEqual(videos(f, "/screen")[0]["radius"], 6.0)    # the scene's cover, split rounding

    def test_vertical_stacks_and_keeps_the_card_inside_its_pane(self):
        data = self.split()
        data["outputs"] = [{"name": "vertical", "size": "1080x1920"}]
        p = plan(data, self.d, "vertical")
        f = frame_at(p, 5.0)
        x, y, w, h = videos(f, "/camera")[0]["dst"]
        self.assertGreater(y, 1000)                                     # the lower pane
        self.assertLessEqual(y + h, 1920)

    def test_position_and_shadow(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera", "fit": "inset", "bubble": False,
                              "inset": {"aspect": 1, "position": [0, 0], "shadow": 0.5}}]}, self.d)
        f = frame_at(p, 2.0)
        x, y, w, h = videos(f, "/camera")[0]["dst"]
        pad = 0.06 * 1080
        self.assertAlmostEqual(x, pad, places=0)                        # top-left of the room
        self.assertAlmostEqual(y, pad, places=0)
        self.assertAlmostEqual(w, h, places=0)
        types = [l["type"] for l in f["layers"]]
        self.assertLess(types.index("shadow"), types.index("video"))    # under the card
        self.assertAlmostEqual([l for l in f["layers"] if l["type"] == "shadow"][0]["opacity"], 0.3)

    def test_no_shadow_by_default(self):
        p = plan(self.split(), self.d)
        self.assertFalse([l for l in frame_at(p, 5.0)["layers"] if l["type"] == "shadow"])

    def test_image_backdrop_is_cover_cropped(self):
        from PIL import Image
        Image.new("RGB", (400, 400), (0, 80, 160)).save(os.path.join(self.d, "stage.png"))
        p = plan({"style": {"backdrop": {"image": "stage.png"}},
                  "scenes": [{"at": 0, "shot": "camera", "fit": "inset", "bubble": False}]}, self.d)
        img = [l for l in frame_at(p, 2.0)["layers"] if l["type"] == "image"][0]
        self.assertEqual(img["dst"], [0.0, 0.0, 1920.0, 1080.0])
        rx, ry, rw, rh = img["rect"]
        self.assertAlmostEqual(rw / rh, 16 / 9, places=2)               # a 16:9 slice of the square
        self.assertIn(os.path.join(self.d, "stage.png"), p["images"])

    def test_background_can_still_be_blur_or_a_colour(self):
        p = plan(self.split({"background": "#224466"}), self.d)
        fills = [l for l in frame_at(p, 5.0)["layers"] if l["type"] == "fill" and l["color"] == "#224466"]
        self.assertEqual(len(fills), 1)
        p = plan({"scenes": [{"at": 0, "shot": {"window": {"title": "left"}}, "fit": "contain",
                              "background": "backdrop", "bubble": False}]}, self.d)
        self.assertTrue([l for l in frame_at(p, 1.0)["layers"] if l["type"] == "gradient"])

    def test_move_lifts_the_camera_into_a_card(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera", "bubble": False},
                             {"at": 5, "shot": "camera", "fit": "inset", "transition": {"move": 1.0},
                              "bubble": False}]}, self.d)
        self.assertFalse([w for w in p["warnings"] if "move" in w])
        widths = [videos(frame_at(p, t), "/camera")[0]["dst"][2] for t in (4.9, 5.5, 6.5)]
        self.assertGreater(widths[0], widths[1])
        self.assertGreater(widths[1], widths[2])
        grad = [l for l in frame_at(p, 5.5)["layers"] if l["type"] == "gradient"]
        self.assertTrue(grad and 0 < grad[0]["opacity"] < 1)            # the backdrop fades in

    def test_theme_backdrop(self):
        sc = S.parse({"style": {"backdrop": {"gradient": ["#000000", "#ffffff"], "angle": 90}},
                      "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertEqual(sc["style"]["backdrop"]["gradient"], ["#000000", "#ffffff"])
        self.assertIsNone(sc["style"]["backdrop"]["image"])

    def test_validation(self):
        bad = [({"style": {"inset": {"aspect": "2-3"}}}, "inset.aspect"),
               ({"style": {"backdrop": {"gradient": ["#000000"]}}}, "2-4 colours"),
               ({"style": {"inset": {"radiuss": 4}}}, "did you mean 'radius'"),
               ({"scenes_bg": True}, "background"),
               ({"pane_fit": True}, "fit")]
        for extra, msg in bad:
            data = {"scenes": [{"at": 0, "shot": "screen"}]}
            if extra.get("scenes_bg"):
                data["scenes"][0]["background"] = "fancy"
            elif extra.get("pane_fit"):
                data["scenes"][0]["shot"] = {"split": [{"camera": {}, "fit": "insett"}, "screen"]}
            else:
                data.update(extra)
            with self.assertRaises(S.ScriptError) as cm:
                S.parse(data, self.d)
            self.assertIn(msg, str(cm.exception))


class Push(unittest.TestCase):
    """push: a slow push-in while a shot holds (Ken Burns)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def widths(self, p, src="/screen", t0=0.0, t1=10.0):
        return [(f["t"], videos(f, src)[-1]["rect"][2]) for f in p["frames"] if t0 <= f["t"] <= t1]

    def test_curve_ease_in_steady_ease_out(self):
        self.assertEqual(R.push_amount(0, 10), 0.0)
        self.assertEqual(R.push_amount(10, 10), 1.0)
        self.assertAlmostEqual(R.push_amount(5, 10), 0.5)
        self.assertLess(R.push_amount(0.5, 10), 0.5 * 0.5 / 9)     # slower than steady at first
        steps = [R.push_amount(t + 0.1, 10) - R.push_amount(t, 10) for t in (2, 4, 6, 8)]
        self.assertTrue(max(steps) - min(steps) < 1e-9)              # a steady rate in the middle

    def test_screen_push_to_80_percent_over_10s(self):
        p = plan({"scenes": [{"at": 0, "shot": {"rect": [0, 0, 2560, 1080]}, "fit": "cover", "push": True,
                              "bubble": False}]}, self.d)
        w = dict(self.widths(p))
        self.assertAlmostEqual(w[0.0], 2560, delta=1)
        self.assertAlmostEqual(w[9.9], 0.8 * 2560, delta=4)
        ws = [v for _, v in sorted(w.items())]
        self.assertTrue(all(b <= a + 1e-6 for a, b in zip(ws, ws[1:])))      # only ever tighter
        self.assertLess(max(a - b for a, b in zip(ws, ws[1:])), 0.01 * 2560)  # and slowly

    def test_tall_outputs_push_half_as_far(self):
        p = plan({"outputs": [{"name": "vertical", "size": "1080x1920"}],
                  "scenes": [{"at": 0, "shot": {"rect": [0, 0, 1215, 2160]}, "fit": "cover", "push": True,
                              "bubble": False}]}, self.d, "vertical")
        w = dict(self.widths(p))
        self.assertAlmostEqual(w[9.9] / w[0.0], 0.9, delta=0.005)

    def test_explicit_to_and_over(self):
        p = plan({"scenes": [{"at": 0, "shot": {"rect": [0, 0, 2560, 1080]}, "fit": "cover", "bubble": False,
                              "push": {"to": 0.9, "over": 4}}]}, self.d)
        w = dict(self.widths(p))
        self.assertAlmostEqual(w[4.0] / w[0.0], 0.9, delta=0.002)
        self.assertAlmostEqual(w[9.0], w[4.0], delta=0.5)                    # then it holds

    def test_a_cut_inside_does_not_jump_it(self):
        p = plan({"cuts": {"ranges": [[3.0, 6.0]]},
                  "scenes": [{"at": 0, "shot": {"rect": [0, 0, 2560, 1080]}, "fit": "cover", "push": True,
                              "bubble": False}]}, self.d)
        ws = [v for _, v in self.widths(p, t0=0, t1=6.5)]
        self.assertLess(max(a - b for a, b in zip(ws, ws[1:])), 0.01 * 2560)  # 3 s of source skipped, no jump

    def test_camera_and_inset_card(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera", "fit": "inset", "push": True, "bubble": False}]}, self.d)
        a, b = videos(frame_at(p, 0.0), "/camera")[0], videos(frame_at(p, 9.9), "/camera")[0]
        self.assertEqual(a["dst"], b["dst"])                                  # the card stays
        self.assertAlmostEqual(b["rect"][2] / a["rect"][2], 0.8, delta=0.01)  # the picture tightens

    def test_off_by_default_and_false_wins_over_the_style(self):
        base = {"scenes": [{"at": 0, "shot": {"rect": [0, 0, 2560, 1080]}, "fit": "cover", "bubble": False}]}
        w = dict(self.widths(plan(base, self.d)))
        self.assertAlmostEqual(w[9.9], w[0.0])
        base["style"] = {"push": {"to": 0.8}}
        base["scenes"][0]["push"] = False
        w = dict(self.widths(plan(base, self.d)))
        self.assertAlmostEqual(w[9.9], w[0.0])

    def test_cursor_shots_are_left_alone_with_a_warning(self):
        p = plan({"scenes": [{"at": 0, "shot": {"cursor": {"zoom": 2}}, "push": True, "bubble": False}]}, self.d)
        self.assertTrue([w for w in p["warnings"] if "push: only on" in w])

    def test_validation(self):
        for v, msg in ((0.3, "out of range"), ({"too": 0.8}, "did you mean 'to'"), ({"to": 0.8, "over": 0}, "out of range")):
            with self.assertRaises(S.ScriptError) as cm:
                S.parse({"scenes": [{"at": 0, "shot": "screen", "push": v}]}, self.d)
            self.assertIn(msg, str(cm.exception))


class Punch(unittest.TestCase):
    """punch: a quick zoom in on an important point, with a small dip out first."""

    RECT = {"rect": [0, 0, 2560, 1080]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def widths(self, p, src="/screen"):
        return {f["t"]: videos(f, src)[-1]["rect"][2] for f in p["frames"]}

    def scene(self, **kw):
        return {"scenes": [dict({"at": 0, "shot": {"rect": [200, 100, 1920, 810]}, "fit": "cover",
                                 "bubble": False}, **kw)]}

    def test_curve_dips_lands_holds_releases(self):
        lead = R.PUNCH_DIP + R.PUNCH_IN
        self.assertEqual(R.punch_amount(0, 1.5, 0.1), 0.0)
        self.assertAlmostEqual(R.punch_amount(R.PUNCH_DIP, 1.5, 0.1), -0.1)    # the anticipation
        self.assertGreater(max(R.punch_amount(lead - k / 100, 1.5, 0.1) for k in range(1, 10)), 1.0)  # overshoot
        self.assertAlmostEqual(R.punch_amount(lead, 1.5, 0.1), 1.0)
        self.assertEqual(R.punch_amount(lead + 1.0, 1.5, 0.1), 1.0)            # held
        self.assertEqual(R.punch_amount(lead + 1.5 + R.PUNCH_OUT, 1.5, 0.1), 0.0)

    def test_lands_on_at_and_releases(self):
        p = plan(self.scene(punch=3.0), self.d, fps=50)
        w = self.widths(p)
        base = w[0.0]
        self.assertAlmostEqual(w[3.0] / base, 0.94, delta=0.002)              # 6% in, on the word
        self.assertAlmostEqual(w[4.4] / base, 0.94, delta=0.002)              # held 1.5 s
        self.assertLess(min(w.values()), 0.94 * base)                         # a slight overshoot
        self.assertAlmostEqual(max(w.values()) / base, 1.006, delta=0.001)      # dip out first
        self.assertEqual(w[6.0], base)
        dip_t = max(w, key=w.get)
        self.assertAlmostEqual(dip_t, 3.0 - R.PUNCH_IN, delta=0.03)

    def test_list_and_per_punch_settings(self):
        p = plan(self.scene(punch=[2.0, {"at": 5.0, "zoom": 0.2, "hold": 0.5, "dip": 0}]), self.d, fps=50)
        w = self.widths(p)
        self.assertAlmostEqual(w[2.0] / w[0.0], 0.94, delta=0.002)
        self.assertAlmostEqual(w[5.2] / w[0.0], 0.8, delta=0.002)
        self.assertLessEqual(max(w[t] for t in w if t > 4), w[0.0])           # dip: 0 = none

    def test_dip_stays_inside_the_source(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen", "fit": "cover", "punch": 3.0, "bubble": False}]},
                 self.d, fps=50)
        for f in p["frames"]:
            x, y, w, h = videos(f, "/screen")[-1]["rect"]
            self.assertTrue(x >= -0.5 and y >= -0.5 and x + w <= 5120.5 and y + h <= 2160.5)

    def test_camera_inset_card(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera", "fit": "inset", "punch": 3.0, "bubble": False}]}, self.d)
        a, b = videos(frame_at(p, 0.0), "/camera")[0], videos(frame_at(p, 3.0), "/camera")[0]
        self.assertEqual(a["dst"], b["dst"])
        self.assertAlmostEqual(b["rect"][2] / a["rect"][2], 0.94, delta=0.01)

    def test_runs_on_the_output_clock_across_a_cut(self):
        cut = self.widths(plan(dict(self.scene(punch={"at": 3.0, "hold": 1.0}), cuts={"ranges": [[3.5, 6.0]]}),
                               self.d, fps=50))
        whole = self.widths(plan(self.scene(punch={"at": 3.0, "hold": 1.0}), self.d, fps=50))
        for t in sorted(cut):   # the same move frame by frame: the cut doesn't jump or end it
            if t < 5.0:
                self.assertAlmostEqual(cut[t], whole[t], delta=0.5)

    def test_off_by_default_and_style_defaults(self):
        w = self.widths(plan(self.scene(), self.d))
        self.assertEqual(len(set(w.values())), 1)
        data = self.scene(punch=3.0)
        data["style"] = {"punch": {"zoom": 0.15}}
        w = self.widths(plan(data, self.d, fps=50))
        self.assertAlmostEqual(w[3.0] / w[0.0], 0.85, delta=0.002)

    def test_per_output(self):
        data = dict(self.scene(punch=3.0), outputs=[{"name": "main", "size": "1920x1080"},
                                                     {"name": "vertical", "size": "1080x1920"}])
        data["scenes"][0]["vertical"] = {"punch": False}
        w = self.widths(plan(data, self.d, "vertical"))
        self.assertEqual(len(set(w.values())), 1)

    def test_warnings(self):
        p = plan({"scenes": [{"at": 0, "shot": {"cursor": {"zoom": 2}}, "punch": 2, "bubble": False}]}, self.d)
        self.assertTrue([w for w in p["warnings"] if "punch: only on" in w])
        p = plan({"scenes": [{"at": 0, "shot": "screen", "punch": 6, "bubble": False},
                             {"at": 5, "shot": "camera"}]}, self.d)
        self.assertTrue([w for w in p["warnings"] if "after the scene ends" in w])
        p = plan({"scenes": [{"at": 0, "shot": "camera", "punch": 2, "push": True, "bubble": False}]}, self.d)
        self.assertTrue([w for w in p["warnings"] if "punch and push" in w])

    def test_validation(self):
        for v, msg in ((-1, "out of range"), ({"at": 2, "zoom": 0.9}, "out of range"),
                       ({"at": 2, "zom": 0.1}, "did you mean 'zoom'"), ({"zoom": 0.1}, "missing 'at'"),
                       ("x", "expected a number")):
            with self.assertRaises(S.ScriptError) as cm:
                S.parse({"scenes": [{"at": 0, "shot": "screen", "punch": v}]}, self.d)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"scenes": [{"at": 4, "shot": "screen", "punch": 2}]}, self.d)
        self.assertIn("before the scene starts", str(cm.exception))


class Callouts(unittest.TestCase):
    """callouts: small animated info boxes - draw (border traces itself), bar, pill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def data(self, *callouts, **style):
        d = {"scenes": [{"at": 0, "shot": "screen", "bubble": False}], "callouts": list(callouts)}
        if style:
            d["style"] = {"callout": style}
        return d

    def callout_layers(self, f):
        return [l for l in f["layers"] if l["type"] == "stroke" or l.get("align") == "left"
                or (l["type"] == "fill" and l.get("shape") == "round")]

    def resolver(self, *callouts, **style):
        sc = S.parse(self.data(*callouts, **style), self.d)
        return R.OutputResolver(sc, sc["outputs"][0], R.load_sessions(sc), []), sc["callouts"]

    def check_error(self, data, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse(data, self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation(self):
        self.check_error(self.data({"at": 1, "title": "Owner", "look": "pill", "text": "two"}), "a pill is one short label")
        self.check_error(self.data({"at": 1, "title": "x", "text": "a\nb\nc\nd"}), "at most 3 lines")
        self.check_error(self.data({"at": 1, "title": "x", "duration": 0.8}), "shorter than in + out")
        self.check_error(self.data({"at": 1, "title": "x", "look": "drawn"}), "expected one of draw, bar, pill")
        self.check_error(self.data({"at": 1, "title": "x", "form": "left"}), "did you mean 'from'?")
        self.check_error(self.data({"at": 1}), "missing 'title'")

    def test_no_callouts_no_layers(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen", "bubble": False}]}, self.d)
        self.assertFalse(any(self.callout_layers(f) for f in p["frames"]))

    def test_rounded_path_and_part_of_it(self):
        pts = R.round_rect_path(10, 20, 200, 100, 10)
        ring = pts + [pts[0]]
        length = sum(math.dist(a, b) for a, b in zip(ring, ring[1:]))
        self.assertAlmostEqual(length, 2 * (200 + 100) - (8 - 2 * math.pi) * 10, delta=0.5)
        self.assertEqual(pts[0], (20, 20))                                   # starts where the top-left corner ends
        half, closed = R.path_part(pts, 0.5)
        self.assertFalse(closed)
        self.assertAlmostEqual(sum(math.dist(a, b) for a, b in zip(half, half[1:])), length / 2, delta=0.5)
        self.assertTrue(R.path_part(pts, 1.0)[1])

    def test_appears_at_its_time_and_goes(self):
        p = plan(self.data({"at": 2.0, "duration": 3.0, "title": "Reports", "text": "Exports and schedules"}),
                 self.d, fps=50)
        self.assertFalse(self.callout_layers(frame_at(p, 1.98)))
        self.assertTrue(self.callout_layers(frame_at(p, 2.0)))
        self.assertTrue(self.callout_layers(frame_at(p, 4.98)))
        self.assertFalse(self.callout_layers(frame_at(p, 5.02)))

    def test_draw_traces_the_border_then_fades_the_text_in(self):
        res, (c,) = self.resolver({"at": 2.0, "title": "Reports", "text": "Exports and schedules", "look": "draw"})

        def parts(e):
            ls = res.callout(c, e)
            stroke = next(l for l in ls if l["type"] == "stroke")
            texts = [l for l in ls if l["type"] == "text"]
            return stroke, texts
        s0, t0 = parts(0.1)
        self.assertFalse(s0["closed"])
        self.assertEqual(t0[0]["opacity"], 0.0)                              # text waits for the border
        s1, t1 = parts(1.0)
        self.assertTrue(s1["closed"])
        self.assertEqual([t["opacity"] for t in t1], [1.0, 0.92])             # the detail line a touch softer
        self.assertLess(parts(0.4)[1][0]["y"] - t1[0]["y"], 0.3 * t1[0]["size"] + 0.1)  # rises a little, into place
        self.assertGreater(parts(0.4)[1][0]["y"], t1[0]["y"])
        _, late = parts(c["duration"] - 0.1)
        self.assertLess(late[0]["opacity"], 0.5)                              # fading out

    def test_direction_of_travel(self):
        res, (c,) = self.resolver({"at": 0, "title": "x", "from": "left", "to": "up", "distance": 0.05})
        rest = next(l for l in res.callout(c, 1.5) if l["type"] == "fill")["dst"]
        start = next(l for l in res.callout(c, 0.0) if l["type"] == "fill")["dst"]
        end = next(l for l in res.callout(c, c["duration"] - 0.01) if l["type"] == "fill")["dst"]
        self.assertAlmostEqual(rest[0] - start[0], 0.05 * 1080, delta=1)      # came in from the left
        self.assertEqual(start[1], rest[1])
        self.assertLess(end[1], rest[1] - 0.04 * 1080)                        # leaves upwards
        res2, (c2,) = self.resolver({"at": 0, "title": "x", "corner": "top-right"})
        self.assertGreater(next(l for l in res2.callout(c2, 0.0) if l["type"] == "fill")["dst"][0],
                           next(l for l in res2.callout(c2, 1.5) if l["type"] == "fill")["dst"][0])  # auto: from its side

    def test_bar_grows_and_text_slides_out_from_behind_it(self):
        res, (c,) = self.resolver({"at": 0, "title": "Editor", "text": "Can change anything", "look": "bar"})

        def bar(e):
            return next(l for l in res.callout(c, e) if l["type"] == "fill" and l.get("shape") != "round")
        self.assertLess(bar(0.1)["dst"][3], bar(1.0)["dst"][3])
        texts = [l for l in res.callout(c, 0.4) if l["type"] == "text"]
        self.assertTrue(all("clip" in t for t in texts))
        self.assertLess(texts[0]["x"], [l for l in res.callout(c, 1.0) if l["type"] == "text"][0]["x"])

    def test_pill_is_round_and_one_line(self):
        res, (c,) = self.resolver({"at": 0, "title": "Admin", "look": "pill"})
        ls = res.callout(c, 1.0)
        fill = next(l for l in ls if l["type"] == "fill")
        self.assertAlmostEqual(fill["radius"], fill["dst"][3] / 2, delta=0.1)
        self.assertEqual(len([l for l in ls if l["type"] == "text"]), 1)

    def test_theme_style_and_per_callout_override(self):
        res, (a, b) = self.resolver({"at": 0, "title": "x"}, {"at": 9, "title": "y", "look": "pill"},
                                    look="bar", background="#0d1b24")
        self.assertEqual((a["look"], b["look"]), ("bar", "pill"))
        self.assertEqual(b["background"], "#0d1b24")

    def test_section_wipes_across_then_titles(self):
        res, (c,) = self.resolver({"at": 0, "title": "Settings", "text": "One new page", "look": "section",
                                   "from": "right"})
        early, done = res.section(c, 0.2, 0)[0], res.section(c, 1.5, 0)[0]
        self.assertEqual(done["dst"], [0, 0, 1920, 1080])
        self.assertNotIn("clip", done)
        self.assertGreater(early["clip"][0], 960)                            # from the right: the right part first
        self.assertEqual(early["clip"][0] + early["clip"][2], 1920)
        title = lambda e: next(l for l in res.section(c, e, 0) if l["type"] == "text")
        self.assertEqual(title(0.1)["opacity"], 0.0)
        self.assertEqual(title(1.5)["opacity"], 1.0)
        self.assertLess(res.section(c, c["duration"] - 0.05, 0)[0]["opacity"], 0.2)   # fades back to the video

    def test_section_sparkle_needs_the_theme(self):
        self.check_error(self.data({"at": 0, "title": "x", "look": "section", "sparkle": True}),
                         "a theme that has a `sparkle:` section")
        d = self.data({"at": 0, "title": "x", "sparkle": True})
        d["style"] = {"sparkle": {"count": 12}}
        self.check_error(d, "only a section's wipe can sparkle")
        d = self.data({"at": 0, "title": "x", "look": "section", "sparkle": True})
        d["style"] = {"sparkle": {"count": 12}}
        sc = S.parse(d, self.d)
        res = R.OutputResolver(sc, sc["outputs"][0], R.load_sessions(sc), [])
        mid = res.section(sc["callouts"][0], 0.35, 0)
        self.assertTrue(any(l["type"] == "star" for l in mid))                # the trail
        self.assertLess(mid[0]["clip"][2], 1920)                              # revealed behind the sweep

    def test_pause_holds_the_recording_while_the_callout_comes_in(self):
        p = plan(self.data({"at": 3.0, "title": "Settings", "look": "section", "pause": 1.0}), self.d, fps=10)
        base = plan(self.data({"at": 3.0, "title": "Settings", "look": "section"}), self.d, fps=10)
        self.assertAlmostEqual(p["duration"], base["duration"] + 1.0, places=3)   # the pause adds time
        held = [f for f in p["frames"] if f.get("hold")]
        self.assertEqual(len(held), 10)
        self.assertTrue(all(f["st"] == 3.0 and f["src"] == "main" for f in held))   # one moment, still
        after = p["frames"][p["frames"].index(held[-1]) + 1]
        self.assertAlmostEqual(after["st"], 3.0, places=3)                     # then it carries on from there
        self.assertNotIn("hold", after)
        # the section animates through the hold: its title is further along at the end than the start
        title = lambda f: next(l for l in f["layers"] if l["type"] == "text" and l["words"][0]["w"] == "Settings")
        self.assertLess(title(held[0])["opacity"], title(held[-1])["opacity"])

    def test_section_titles_follow_the_cards_title_case(self):
        d = self.data({"at": 0, "title": "The live site", "look": "section"}, {"at": 9, "title": "the live site"})
        d["style"] = {"card": {"title_case": True}}
        a, b = S.parse(d, self.d)["callouts"]
        self.assertEqual(a["title"], "The Live Site")
        self.assertEqual(b["title"], "The Live Site")                          # a box's title too
        d = self.data({"at": 0, "title": "Settings vs profile", "text": "both in one place."},
                      {"at": 9, "title": "AC-42 · reports"})
        d["style"] = {"card": {"title_case": True}}
        e, f = S.parse(d, self.d)["callouts"]
        self.assertEqual((e["title"], e["lines"]), ("Settings vs Profile", ["both in one place."]))  # text as written
        self.assertEqual(f["title"], "AC-42 · Reports")                        # codes stay as written

    def test_overlap_in_one_corner_warns(self):
        p = plan(self.data({"at": 1, "title": "a"}, {"at": 3, "title": "b"},
                           {"at": 3, "title": "c", "corner": "top-right"}), self.d)
        self.assertEqual(len([w for w in p["warnings"] if "overlap" in w]), 1)


class Captions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)
        add_transcript(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lines_break_at_sentence_and_pause(self):
        sess = R.Session(self.d)
        lines = R.caption_lines(sess.transcript, max_words=6)
        self.assertEqual([" ".join(w[2] for w in l[2]) for l in lines], ["So this is the demo.", "Now um the editor"])
        self.assertLess(lines[0][1], lines[1][0])            # no overlap

    def test_text_layer_highlights_the_spoken_word(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        texts = [l for l in frame_at(p, 1.6)["layers"] if l["type"] == "text"]
        self.assertEqual(len(texts), 1)
        self.assertEqual([w["w"] for w in texts[0]["words"] if w["hi"]], ["is"])
        self.assertFalse([l for l in frame_at(p, 4.0)["layers"] if l["type"] == "text"])  # the pause: none

    def test_caption_fix_keeps_punctuation(self):
        self.assertEqual(R.fix_word("Akme,", {"Akme": "Acme"}), "Acme,")
        self.assertEqual(R.fix_word("Akmes", {"Akme": "Acme"}), "Akmes")   # whole words only
        p = plan({"style": {"captions": {"fix": {"demo": "Acme"}}}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        texts = [l for l in frame_at(p, 2.0)["layers"] if l["type"] == "text"]
        self.assertIn("Acme.", [w["w"] for w in texts[0]["words"]])   # "demo." -> "Acme."

    def test_captions_off(self):
        p = plan({"style": {"captions": False}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertFalse([l for f in p["frames"] for l in f["layers"] if l["type"] == "text"])

    def test_cuts_from_transcript(self):
        p = plan({"cuts": {"pauses": 1.2, "fillers": True}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        # The 3 s pause (2.5-5.5) minus 0.3 kept each side = 2.4 s, and "um" (0.25 + 0.1 margin).
        self.assertAlmostEqual(p["duration"], 10.0 - 2.4 - 0.35, places=2)
        self.assertEqual(len(p["segments"]), 3)


class Themes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "s"))
        self.d = make_session(os.path.join(self.tmp.name, "s"))
        add_transcript(self.d)
        self.cfg = os.path.join(self.tmp.name, "cfg")
        os.makedirs(os.path.join(self.cfg, "pugrec", "themes"))
        with open(os.path.join(self.cfg, "pugrec", "themes", "acme.yaml"), "w") as f:
            f.write("description: Acme Corp\naccent: '#0055ff'\ncaptions: {color: '#ffffff', font: DejaVu Sans}\n"
                    "card: {background: '#002244'}\nbubble: {corner: top-right}\n")
        from unittest import mock
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.cfg})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_theme_loads_and_edit_overrides(self):
        sc = S.parse({"style": {"theme": "acme", "captions": {"color": "#eeeeee"}}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        st = sc["style"]
        self.assertEqual(st["accent"], "#0055ff")                    # from the theme
        self.assertEqual(st["captions"]["font"], "DejaVu Sans")      # from the theme
        self.assertEqual(st["captions"]["color"], "#eeeeee")         # edit.yaml wins
        self.assertEqual(st["captions"]["size"], 0.045)              # untouched default
        self.assertEqual(st["bubble"]["corner"], "top-right")

    def test_fix_maps_merge_and_multiword(self):
        with open(os.path.join(self.cfg, "pugrec", "themes", "fp.yaml"), "w") as f:
            f.write("vocabulary: [FieldPal]\ncaptions: {fix: {'Field Pal': FieldPal}}\n")
        sc = S.parse({"style": {"theme": "fp", "captions": {"fix": {"Akme": "Acme"}}},
                      "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        fixes = sc["style"]["captions"]["fix"]
        self.assertEqual(fixes, {"Field Pal": "FieldPal", "Akme": "Acme"})  # merged, not replaced
        words = [(0.0, 0.4, "the"), (0.5, 0.7, "field"), (0.7, 0.9, "pal,"), (1.0, 1.2, "akme."), (1.3, 1.4, "pal")]
        out = R.apply_fixes(words, fixes)
        self.assertEqual([w[2] for w in out], ["the", "FieldPal,", "Acme.", "pal"])  # case-insensitive, punctuation kept
        self.assertEqual(out[1][:2], (0.5, 0.9))                                     # spans both words' times
        self.assertEqual(S.theme_vocabulary("fp"), ["FieldPal"])

    def test_vocabulary_sources_combine(self):
        from unittest import mock
        from pugrec import transcript
        with open(os.path.join(self.cfg, "pugrec", "themes", "fp.yaml"), "w") as f:
            f.write("vocabulary: [FieldPal, Acme]\n")
        with mock.patch("pugrec.recorder.config", return_value={"vocabulary": ["PugPal", "acme"]}):
            self.assertEqual(transcript.vocabulary("Hyprland, FieldPal", "fp"), ["PugPal", "acme", "FieldPal", "Hyprland"])

    def test_unknown_theme_lists_available(self):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"style": {"theme": "acmee"}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertIn("unknown theme 'acmee' (available: acme)", str(cm.exception))

    def test_theme_keys_validated(self):
        with open(os.path.join(self.cfg, "pugrec", "themes", "bad.yaml"), "w") as f:
            f.write("accnt: '#000000'\n")
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"style": {"theme": "bad"}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertIn("theme bad: unknown key 'accnt' (did you mean 'accent'?)", str(cm.exception))

    def test_accent_reaches_rings_and_captions(self):
        p = plan({"style": {"theme": "acme"}, "scenes": [{"at": 0, "shot": {"window": {"title": "right"}}, "fit": "cover"}]}, self.d)
        rings = [l for l in frame_at(p, 2.1)["layers"] if l["type"] == "ring"]
        texts = [l for l in frame_at(p, 1.6)["layers"] if l["type"] == "text"]
        self.assertEqual(rings[0]["color"], "#0055ff")
        self.assertEqual((texts[0]["hi"], texts[0]["color"], texts[0]["font"]), ("#0055ff", "#ffffff", "DejaVu Sans"))

    def test_defaults_round_trip_as_a_theme(self):
        import contextlib, io
        from pugrec import __main__ as cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["themes", "--defaults"])
        with open(os.path.join(self.cfg, "pugrec", "themes", "pugpal.yaml"), "w") as f:
            f.write(buf.getvalue())
        a = S.parse({"style": {"theme": "pugpal"}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)["style"]
        b = S.parse({"scenes": [{"at": 0, "shot": "screen"}]}, self.d)["style"]
        a.pop("theme"), b.pop("theme")
        self.assertEqual(a, b)                                        # pugpal.yaml = today's look exactly

    def test_logo_and_title_cards(self):
        from PIL import Image
        logo = os.path.join(self.tmp.name, "logo.png")
        Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)
        p = plan({"style": {"theme": "acme", "logo": {"file": logo, "corner": "top-right", "size": 0.1}},
                  "intro": {"title": "Acme demo", "subtitle": "Q4", "duration": 2},
                  "outro": "Thanks",
                  "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertAlmostEqual(p["duration"], 2 + 10 + 3)              # intro + recording + outro (style default 3 s)
        first = frame_at(p, 1.0)
        self.assertEqual(first["src"], "card:intro")
        texts = [l["words"][0]["w"] for l in first["layers"] if l["type"] == "text"]
        self.assertEqual(texts, ["Acme demo", "Q4"])
        self.assertEqual(first["layers"][0]["color"], "#002244")      # the theme's card background
        imgs = [l for l in frame_at(p, 5.0)["layers"] if l["type"] == "image"]
        self.assertEqual(len(imgs), 1)
        x, y, w, h = imgs[0]["dst"]
        self.assertAlmostEqual(w, 192.0)                              # 10% of 1920 wide...
        self.assertAlmostEqual(h, 96.0)                               # ...keeping the 2:1 aspect
        self.assertGreater(x, 1600)                                   # top-right
        self.assertIn(logo, p["images"])
        self.assertEqual(frame_at(p, 14.0)["src"], "card:outro")

    def test_title_case(self):
        self.assertEqual(S.title_case("decision and gated cards"), "Decision and Gated Cards")
        self.assertEqual(S.title_case("the FieldPal app for ACME, AC-42"), "The FieldPal App for ACME, AC-42")
        self.assertEqual(S.title_case("workflow: start-and-end tasks in"), "Workflow: Start-And-End Tasks In")
        self.assertEqual(S.title_case("what's new"), "What's New")

    def test_card_title_case_is_a_style_option(self):
        data = {"intro": "decision and gated cards", "outro": {"title": "see you in the app"},
                "scenes": [{"at": 0, "shot": "screen"}]}
        title = lambda p, t: [l for l in frame_at(p, t)["layers"] if l["type"] == "text"][0]["words"][0]["w"]
        p = plan(data, self.d)
        self.assertEqual(title(p, 1.0), "decision and gated cards")     # off by default: as written
        p = plan({**data, "style": {"card": {"title_case": True}}}, self.d)
        self.assertEqual(title(p, 1.0), "Decision and Gated Cards")
        self.assertEqual(title(p, 14.5), "See You in the App")
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"style": {"card": {"title_case": "yes"}}, "scenes": [{"at": 0, "shot": "screen"}]})
        self.assertIn("true or false", str(cm.exception))

    def test_card_fades(self):
        def ops(f):
            return [l.get("opacity", 1) for l in f["layers"] if l["type"] in ("text", "image")]
        p = plan({"intro": "Hi", "outro": "Bye", "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertEqual(set(ops(p["frames"][0])), {1.0})              # intro frame 0 = the thumbnail, fully drawn
        self.assertLess(max(ops(frame_at(p, 2.9))), 0.5)              # the intro still fades out
        self.assertLess(max(ops(frame_at(p, 13.05))), 0.5)             # the outro still fades in
        p = plan({"style": {"card": {"fade_in": 0.5, "fade_out": 0}}, "intro": "Hi",
                  "outro": {"title": "Bye", "fade_in": 0},
                  "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertEqual(max(ops(p["frames"][0])), 0.0)                # a theme can bring the fade back
        self.assertEqual(set(ops(frame_at(p, 2.9))), {1.0})           # fade_out 0: drawn to the cut
        self.assertEqual(set(ops(frame_at(p, 13.01))), {1.0})          # per card wins

    def test_theme_cards_and_card_icon(self):
        from PIL import Image
        tdir = os.path.join(self.cfg, "pugrec", "themes")
        Image.new("RGBA", (100, 100), (0, 0, 255, 255)).save(os.path.join(tdir, "icon.png"))
        with open(os.path.join(tdir, "brand.yaml"), "w") as f:
            f.write("outro: {title: 'Thanks', subtitle: brand.example, duration: 2}\n"
                    "card: {image: icon.png, image_size: 0.2}\n")       # icon relative to the theme file
        base = {"style": {"theme": "brand"}, "scenes": [{"at": 0, "shot": "screen"}]}
        p = plan(base, self.d)
        self.assertAlmostEqual(p["duration"], 10 + 2)                  # the theme's outro
        last = frame_at(p, 11.0)
        self.assertEqual(last["src"], "card:outro")
        icon = [l for l in last["layers"] if l["type"] == "image"]
        self.assertEqual(len(icon), 1)
        x, y, w, h = icon[0]["dst"]
        self.assertAlmostEqual(h, 0.2 * 1080)                          # image_size of the shorter side
        self.assertAlmostEqual(x + w / 2, 960)                         # centred
        title_y = [l for l in last["layers"] if l["type"] == "text"][0]["y"]
        self.assertLess(y + h, title_y)                                # above the title
        own = plan(dict(base, outro="Bye"), self.d)                    # the edit's own outro wins
        self.assertEqual([l["words"][0]["w"] for l in frame_at(own, 11.0)["layers"] if l["type"] == "text"][0], "Bye")
        off = plan(dict(base, outro=False), self.d)                    # outro: false = none
        self.assertAlmostEqual(off["duration"], 10)

    def test_sparkle_only_with_a_theme_that_has_it(self):
        scenes = [{"at": 0, "shot": "screen"}, {"at": 5, "shot": "camera", "transition": {"sparkle": 0.8}}]
        for style in ({}, {"theme": "acme"}):                           # plain, and another theme
            with self.assertRaises(S.ScriptError) as cm:
                S.parse({"style": style, "scenes": scenes}, self.d)
            self.assertIn("only work with a theme that has a `sparkle:` section", str(cm.exception))
        with self.assertRaises(S.ScriptError):                         # not as a style default either
            S.parse({"style": {"transition": {"sweep": 1}}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        S.parse({"style": {"sparkle": {"colors": ["#ffffff"]}}, "scenes": scenes}, self.d)  # configured: fine

    def test_sweep_reveals_behind_the_sprite_with_stars(self):
        from PIL import Image
        sprite = os.path.join(self.tmp.name, "sprite.png")
        Image.new("RGBA", (200, 100), (255, 0, 255, 255)).save(sprite)
        data = {"style": {"sparkle": {"colors": ["#ec1d83", "#3eafe0"], "image": sprite, "image_size": 0.3,
                                      "pixelated": True}},
                "scenes": [{"at": 0, "shot": "screen", "bubble": False},
                           {"at": 5, "shot": "camera", "transition": {"sweep": 1.0}},
                           {"at": 8, "shot": "screen", "transition": {"sparkle": 0.8}, "bubble": False}]}
        p = plan(data, self.d)
        mid = frame_at(p, 5.5)["layers"]
        clipped = [l for l in mid if l.get("clip")]
        sprite_l = [l for l in mid if l["type"] == "image" and l.get("pixelated")]
        stars = [l for l in mid if l["type"] == "star"]
        self.assertTrue(clipped and all(l["src"].endswith("/camera") for l in clipped if l["type"] == "video"))
        self.assertEqual(len(sprite_l), 1)
        edge = clipped[0]["clip"][2]
        x, y, w, h = sprite_l[0]["dst"]
        self.assertAlmostEqual(x + w / 2, edge, delta=2)                # revealed up to the sprite's middle
        self.assertTrue(stars and {s["color"] for s in stars} <= {"#ec1d83", "#3eafe0"})
        self.assertFalse([l for l in frame_at(p, 6.5)["layers"] if l.get("clip") or l["type"] == "star"])  # done
        burst = [l for l in frame_at(p, 8.3)["layers"] if l["type"] == "star"]
        self.assertTrue(burst)
        again = [l for l in frame_at(plan(data, self.d), 8.3)["layers"] if l["type"] == "star"]
        self.assertEqual(burst, again)                                 # deterministic: preview == build

    def test_card_audio_is_silence_in_place(self):
        from pugrec.edit import compose
        p = plan({"intro": {"title": "Hi", "duration": 1}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        p["audio"] = [{"source": "main", "file": "/x/mic-clean.flac"}]
        _, filt = compose.audio_args(p, p["frames"], 1)
        self.assertTrue(filt.startswith("anullsrc=r=48000:cl=mono,atrim=duration=1.0000"))  # intro first
        self.assertIn("[1:a:0]atrim=start=0.0000", filt)                                     # then the recording


class StartMarker(unittest.TestCase):
    def test_edit_begins_after_the_countdown(self):
        from pugrec.edit import draft
        with tempfile.TemporaryDirectory() as d:
            make_session(d)
            with open(os.path.join(d, "session.jsonl"), "a") as f:
                f.write(json.dumps({"t": int(1.5 * SEC), "type": "marker", "name": "start", "countdown": 3}) + "\n")
            p = plan({"cuts": [[4, 5]], "scenes": [{"at": 0, "shot": "screen"}]}, d)
            self.assertAlmostEqual(p["frames"][0]["st"], 1.5)          # warm-up skipped
            self.assertAlmostEqual(p["duration"], 10 - 1.5 - 1.0)
            p = plan({"timeline": [{"source": "main", "to": 3}], "scenes": [{"at": 0, "shot": "screen"}]}, d)
            self.assertAlmostEqual(p["duration"], 1.5)                 # from defaults to the marker
            path, _ = draft.draft(d)
            self.assertEqual(S.load(path)["scenes"][0]["at"], 1.5)    # the intro starts there too


class Draft(unittest.TestCase):
    def test_draft_follows_focus_and_validates(self):
        from pugrec.edit import draft
        with tempfile.TemporaryDirectory() as d:
            make_session(d)
            path, n = draft.draft(d)
            sc = S.load(path)                                      # the draft is a valid script
            shots = [s["shot"] for s in sc["scenes"]]
            self.assertEqual(shots[0]["kind"], "camera")           # intro
            addrs = [s["select"].get("address") for s in shots if s["kind"] == "window"]
            self.assertEqual(addrs[0], "0xb")                      # focused right window first...
            self.assertEqual(addrs[-1], "0xa")                     # ...then the left one (focus at 7 s)
            self.assertTrue(path.endswith("edit.yaml"))
            self.assertEqual(sc["style"]["spotlight"]["window"], {"address": "0xb"})  # the most-focused window
            path2, _ = draft.draft(d)
            self.assertTrue(path2.endswith("edit.draft.yaml"))     # never overwrites an edit

    def test_ignore_is_anchored(self):
        from pugrec.edit.draft import IGNORE_CLASS
        self.assertFalse(IGNORE_CLASS.match("chrome-eoficlgicibekocmfdomjbfnjmehnhcd-Profile_6"))
        self.assertTrue(IGNORE_CLASS.match("com.danklinux.dms"))
        self.assertTrue(IGNORE_CLASS.match("rofi"))


if __name__ == "__main__":
    unittest.main()
