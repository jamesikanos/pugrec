"""Image sources: a still (a page capture) shown as a shot or a split pane, timed by the session."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

from test_edit import frame_at, make_session, plan  # noqa: E402
from pugrec.edit import script as S  # noqa: E402

V = [{"name": "main", "size": "1920x1080"}, {"name": "vertical", "size": "1080x1920"}]
SOURCES = {"main": {"session": "."}, "page": {"image": "page.png"}, "wide": {"image": "wide.png"}}


def pics(f):
    return [l for l in f["layers"] if l["type"] in ("image", "video") and not l.get("bg")]


class ImageSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        make_session(self.d)
        Image.new("RGB", (1200, 6000), (40, 200, 40)).save(os.path.join(self.d, "page.png"))
        Image.new("RGB", (1600, 900), (200, 40, 40)).save(os.path.join(self.d, "wide.png"))

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, scenes, output="main", **extra):
        return plan(dict({"outputs": V, "sources": SOURCES, "scenes": scenes}, **extra), self.d, output)

    def err(self, data, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse(dict({"sources": SOURCES}, **data), self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation(self):
        self.err({"scenes": [{"at": 0, "shot": {"source": "nope"}}]}, "no image source 'nope' (have: page, wide)")
        self.err({"scenes": [{"at": 0, "source": "page", "shot": "screen"}]}, "is an image (no clock)")
        self.err({"scenes": [{"at": 0, "shot": {"split": [{"source": "nah"}, "camera"]}}]}, "split[0].source")
        self.err({"scenes": [{"at": 0, "shot": {"source": "page", "scroll": {"from": 2}}}]}, "scroll.from")
        self.err({"scenes": [{"at": 0, "shot": "screen", "main": {"shot": {"source": "x"}}}]},
                 "scenes[0].main.shot.source")
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"sources": {"page": {"image": "page.png"}}, "scenes": [{"at": 0, "shot": "screen"}]}, self.d)
        self.assertIn("at least one session", str(cm.exception))

    def test_still_as_a_shot_from_the_top(self):
        p = self.p([{"at": 0, "shot": "screen"}, {"at": 4, "shot": {"source": "page"}, "bubble": False}])
        a, b = pics(frame_at(p, 5))[0], pics(frame_at(p, 9))[0]
        self.assertEqual(a["type"], "image")
        self.assertEqual(a["dst"], [0.0, 0.0, 1920.0, 1080.0])
        self.assertEqual(a["rect"], [0.0, 0.0, 1200.0, 675.0])        # the page's top, the frame's shape
        self.assertEqual(a["rect"], b["rect"])                         # still
        self.assertIn(os.path.join(self.d, "page.png"), p["images"])
        self.assertEqual(p["scenes"][1]["label"], "image page")
        self.assertEqual(p["warnings"], [])                            # move -> image: a quiet fade

    def test_scroll_pans_down_over_the_scene(self):
        p = self.p([{"at": 0, "shot": {"source": "page", "scroll": {"from": 0, "to": 0.5}}}], "vertical")
        ys = [pics(frame_at(p, t))[0]["rect"][1] for t in (0.5, 5, 9.9)]
        self.assertLess(ys[0], ys[1])
        self.assertLess(ys[1], ys[2])
        self.assertLessEqual(ys[2], 0.5 * (6000 - 1200 * 1920 / 1080) + 1)

    def test_split_pane_beside_the_speaker(self):
        p = self.p([{"at": 0, "shot": {"split": [{"source": "page"}, "camera"]}}], "vertical")
        img, cam = pics(frame_at(p, 2))
        self.assertEqual(img["type"], "image")
        self.assertEqual(cam["type"], "video")
        self.assertLess(img["dst"][1], cam["dst"][1])                  # column: the page above
        self.assertEqual(img["shape"], "round")

    def test_wide_image_contained_on_the_backdrop_in_vertical(self):
        p = self.p([{"at": 0, "shot": {"source": "wide"}, "bubble": False}], "vertical")
        f = frame_at(p, 2)
        self.assertNotIn("video", [l["type"] for l in f["layers"]])    # no blurred video behind it
        img = [l for l in f["layers"] if l["type"] == "image"][-1]
        self.assertAlmostEqual(img["dst"][2] / img["dst"][3], 1600 / 900, places=2)

    def test_punch_and_inset_work_on_it(self):
        p = self.p([{"at": 0, "shot": {"source": "wide"}, "fit": "inset", "punch": 3, "bubble": False}])
        before = [l for l in frame_at(p, 1)["layers"] if l["type"] == "image"][-1]
        during = [l for l in frame_at(p, 3.2)["layers"] if l["type"] == "image"][-1]
        self.assertLess(during["rect"][2], before["rect"][2])          # punched in
        self.assertEqual(before["shape"], "round")                     # an inset card
        self.assertFalse([w for w in p["warnings"] if "punch" in w])

    def test_compose_paints_it(self):
        import numpy as np
        from pugrec.edit import compose
        p = self.p([{"at": 0, "shot": {"source": "page"}, "bubble": False}])
        canvas = np.zeros((1080, 1920, 3), np.uint8)
        compose.paint(canvas, frame_at(p, 2)["layers"], {}, p["images"])
        self.assertEqual(tuple(canvas[540, 960]), (40, 200, 40))


if __name__ == "__main__":
    unittest.main()
