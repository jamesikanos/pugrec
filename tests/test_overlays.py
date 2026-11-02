"""overlays: images and text cards over the video (card, full frame, corner, beside the face)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

from test_edit import SEC, frame_at, make_session, plan  # noqa: E402
from pugrec.edit import script as S  # noqa: E402

V = [{"name": "main", "size": "1920x1080"}, {"name": "vertical", "size": "1080x1920"}]
SCENES = [{"at": 0, "shot": "screen", "bubble": False}]


def kinds(f):
    return [l["type"] for l in f["layers"]]


class Overlays(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        make_session(self.d)
        Image.new("RGB", (1600, 1000), (200, 40, 40)).save(os.path.join(self.d, "shot.png"))
        Image.new("RGB", (1200, 6000), (40, 200, 40)).save(os.path.join(self.d, "page.png"))
        Image.new("RGBA", (128, 128), (250, 200, 0, 255)).save(os.path.join(self.d, "emoji.png"))

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, overlays, output=None, scenes=SCENES, **extra):
        return plan(dict({"outputs": V, "scenes": scenes, "overlays": overlays}, **extra), self.d, output)

    def images(self, p, t):
        return [l for l in frame_at(p, t)["layers"] if l["type"] == "image"]

    # -- schema --
    def err(self, overlays, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"scenes": SCENES, "overlays": overlays}, self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation(self):
        self.err([{"at": 1}], "either image: (a file) or text:")
        self.err([{"at": 1, "image": "shot.png", "text": "x"}], "either image")
        self.err([{"at": 1, "image": "nope.png"}], "no such file")
        self.err([{"at": 1, "image": "shot.png", "place": "middle"}], "expected one of card, full, corner, face")
        self.err([{"at": 1, "text": "x", "place": "full"}], "a text overlay is a card or a corner")
        self.err([{"at": 1, "image": "shot.png", "in": {"slide": "sideways"}}], "in.slide")
        self.err([{"at": 1, "image": "shot.png", "in": {"wobble": 1}}], "expected one of slide, fade, pop, none")
        self.err([{"at": 1, "image": "shot.png", "crop": [0.5, 0, 0.6, 1]}], "x + w and y + h at most 1")
        self.err([{"at": 1, "image": "shot.png", "duration": 0.5}], "shorter than in + out")
        self.err([{"at": 1, "image": "emoji.png", "place": "face", "scroll": True}], "scroll pans")
        self.err([{"at": 1, "image": "shot.png", "place": "full", "corner": "top"}], "no corner")
        self.err([{"at": 1, "text": "a\nb\nc\nd"}], "one to three short lines")
        self.err([{"at": 1, "image": "shot.png", "credit": "a\nb"}], "one line")
        self.err([{"at": 1, "image": "shot.png", "main": {"at": 2}}], "overlays[0].main: unknown key 'at'")

    def test_no_overlays_parse_as_before(self):
        self.assertNotIn("overlays", S.parse({"scenes": SCENES}, self.d))
        self.assertNotIn("overlays", S.parse({"scenes": SCENES, "overlays": []}, self.d))

    # -- the three uses --
    def test_reference_card_slides_in_with_a_credit(self):
        p = self.p([{"at": 2, "image": "shot.png", "duration": 2, "credit": "Source: example.com"}], "vertical")
        self.assertEqual(self.images(p, 1.9), [])
        rest = self.images(p, 3.0)[0]
        early = self.images(p, 2.1)[0]
        self.assertEqual(rest["opacity"], 1.0)
        self.assertAlmostEqual(rest["dst"][2], 0.8 * 1080, delta=1)            # size: 0.8 of the width
        self.assertAlmostEqual(rest["dst"][3], 0.8 * 1080 * 1000 / 1600, delta=1)
        self.assertLess(early["dst"][1], rest["dst"][1])                        # top card: slides down
        self.assertEqual(rest["shape"], "round")
        f = frame_at(p, 3.0)
        self.assertIn("shadow", kinds(f))
        credit = [l for l in f["layers"] if l["type"] == "text" and l["words"][0]["w"].startswith("Source")][0]
        self.assertEqual(credit["align"], "left")
        self.assertGreater(credit["y"], rest["dst"][1] + rest["dst"][3])          # under the card
        self.assertEqual(self.images(p, 4.1), [])                                # gone after 2 s
        self.assertIn(os.path.join(self.d, "shot.png"), p["images"])

    def test_text_card_big_type_with_accent_word(self):
        p = self.p([{"at": 1, "text": "This changes\n*everything*", "in": "pop"}])
        f = frame_at(p, 2.5)
        texts = [l for l in f["layers"] if l["type"] == "text"]
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[1]["words"][0]["w"], "everything")
        self.assertIn("c", texts[1]["words"][0])                                 # *word*: the accent colour
        self.assertNotIn("c", texts[0]["words"][0])
        self.assertEqual(texts[0]["weight"], 800)
        fill = [l for l in f["layers"] if l["type"] == "fill"][-1]
        cx = fill["dst"][0] + fill["dst"][2] / 2
        cy = fill["dst"][1] + fill["dst"][3] / 2
        self.assertAlmostEqual(cx, 960, delta=1)                                  # top, centred across
        self.assertLess(cy, 300)
        centre = [l for l in frame_at(self.p([{"at": 1, "text": "Hi", "corner": "center"}]), 2.5)["layers"]
                  if l["type"] == "fill"][-1]
        self.assertAlmostEqual(centre["dst"][1] + centre["dst"][3] / 2, 540, delta=1)
        popping = [l for l in frame_at(p, 1.1)["layers"] if l["type"] == "text"][0]
        self.assertNotEqual(popping["size"], texts[0]["size"])                   # pop: springs in

    def test_long_text_shrinks_inside_size(self):
        p = self.p([{"at": 1, "text": "A very long line of words that would never fit in big type at all"}], "vertical")
        fill = [l for l in frame_at(p, 2.5)["layers"] if l["type"] == "fill"][-1]
        self.assertLessEqual(fill["dst"][2], 0.8 * 1080 + 1)

    def test_emoji_pops_beside_the_face(self):
        with open(os.path.join(self.d, "faces.jsonl"), "w") as f:
            for i in range(101):
                f.write(json.dumps({"t": int(i * 0.1 * SEC), "x": 0.45, "y": 0.3, "w": 0.1, "h": 0.2}) + "\n")
        cam = [{"at": 0, "shot": "camera", "bubble": False, "push": False}]
        p = self.p([{"at": 3, "image": "emoji.png", "place": "face"}], "main", scenes=cam)
        rest = self.images(p, 3.6)[0]
        start = self.images(p, 3.1)[0]
        self.assertGreater(start["dst"][2], rest["dst"][2])                      # pop: overshoots, then settles
        self.assertEqual(self.images(self.p([{"at": 3, "image": "emoji.png", "place": "face"}], "main", scenes=cam), 3.0), [])
        cam_l = [l for l in frame_at(p, 3.6)["layers"] if l["type"] == "video"][-1]
        (rx, ry, rw, rh), (dx, dy, dw, dh) = cam_l["rect"], cam_l["dst"]
        fx = dx + (0.5 * 1920 - rx) * dw / rw                                    # face centre on screen
        fy = dy + (0.4 * 1080 - ry) * dh / rh
        ex, ey = rest["dst"][0] + rest["dst"][2] / 2, rest["dst"][1] + rest["dst"][3] / 2
        self.assertGreater(ex, fx)                                               # on the roomier side
        self.assertLess(ey, fy)                                                  # up by the head
        self.assertEqual(self.images(p, 4.3), [])                                # 1.2 s by default
        self.assertFalse([w for w in p["warnings"] if "no face" in w])

    def test_face_without_a_face_falls_back_with_a_warning(self):
        p = self.p([{"at": 3, "image": "emoji.png", "place": "face"}], "main")
        self.assertTrue(self.images(p, 3.5))
        self.assertTrue([w for w in p["warnings"] if "no face is on screen" in w])

    # -- motion, scroll, per output, pause --
    def test_full_frame_scrolls_a_long_page(self):
        p = self.p([{"at": 1, "image": "page.png", "place": "full", "duration": 5, "scroll": True}], "main")
        a, b = self.images(p, 1.5)[0], self.images(p, 5.5)[0]
        self.assertEqual(a["dst"], [0.0, 0.0, 1920.0, 1080.0])
        self.assertAlmostEqual(a["rect"][3], 1200 * 1080 / 1920, delta=0.2)     # cover: the page's width
        self.assertLess(a["rect"][1], b["rect"][1])                              # panned down
        self.assertLessEqual(b["rect"][1] + b["rect"][3], 6000.1)

    def test_crop_and_per_output_override(self):
        o = {"at": 1, "image": "shot.png", "crop": [0, 0, 1, 0.35], "vertical": {"size": 0.95, "corner": "bottom"}}
        main, vert = self.p([o], "main"), self.p([o], "vertical")
        m, v = self.images(main, 3)[0], self.images(vert, 3)[0]
        self.assertAlmostEqual(m["rect"][3], 350, delta=0.2)
        self.assertAlmostEqual(v["dst"][2], 0.95 * 1080, delta=1)
        self.assertGreater(v["dst"][1], 960)                                     # bottom half

    def test_two_in_the_same_place_warn(self):
        p = self.p([{"at": 1, "image": "shot.png"}, {"at": 2, "text": "Hi", "corner": "top"}], "main")
        self.assertTrue([w for w in p["warnings"] if "overlays[1] starts while overlays[0]" in w])
        p = self.p([{"at": 1, "image": "shot.png"}, {"at": 2, "text": "Hi", "corner": "bottom"}], "main")
        self.assertFalse([w for w in p["warnings"] if "overlap" in w])

    def test_pause_holds_the_recording(self):
        p = self.p([{"at": 2, "image": "shot.png", "pause": 1}], "main")
        holds = [f for f in p["frames"] if f.get("hold")]
        self.assertEqual(len(holds), 10)
        self.assertTrue(all(f["st"] == 2.0 for f in holds))
        self.assertTrue(self.images(p, 2.5))

    def test_cut_start_still_shows(self):
        p = self.p([{"at": 2, "image": "shot.png"}], "main", cuts={"ranges": [[1.5, 3]]})
        self.assertTrue([f for f in p["frames"] if any(l["type"] == "image" for l in f["layers"])])

    def test_drawn_above_video_below_captions_and_callouts(self):
        p = self.p([{"at": 1, "image": "shot.png"}], "main",
                   callouts=[{"at": 1, "title": "Owner"}])
        ks = kinds(frame_at(p, 3))
        self.assertLess(max(i for i, k in enumerate(ks) if k == "video"), ks.index("image"))
        self.assertLess(ks.index("image"), max(i for i, k in enumerate(ks) if k == "text"))

    def test_compose_paints_it(self):
        import numpy as np
        from pugrec.edit import compose
        p = self.p([{"at": 1, "image": "shot.png", "credit": "Example"}], "main")
        canvas = np.zeros((1080, 1920, 3), np.uint8)
        layers = [l for l in frame_at(p, 3)["layers"] if l["type"] != "video"]
        compose.paint(canvas, layers, {}, p["images"])
        img = self.images(p, 3)[0]
        x, y, w, h = (int(v) for v in img["dst"])
        self.assertEqual(tuple(canvas[y + h // 2, x + w // 2]), (40, 40, 200))   # BGR of the red shot


if __name__ == "__main__":
    unittest.main()
