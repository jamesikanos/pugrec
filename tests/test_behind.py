"""behind: the speaker cut out (matte as alpha) standing over another shot - an image, a window, the screen."""

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
SOURCES = {"main": {"session": "."}, "page": {"image": "page.png"}}


class Behind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        make_session(self.d)
        Image.new("RGB", (1200, 3000), (40, 200, 40)).save(os.path.join(self.d, "page.png"))

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, scenes, output="main"):
        return plan({"outputs": V, "sources": SOURCES, "scenes": scenes}, self.d, output)

    def err(self, scenes, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"sources": SOURCES, "scenes": scenes}, self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation(self):
        self.err([{"at": 0, "shot": {"camera": {}, "behind": "camera"}}], "stands over a screen, window or image")
        self.err([{"at": 0, "shot": {"camera": {}, "behind": {"split": ["screen", "screen"]}}}], "stands over")
        self.err([{"at": 0, "shot": {"camera": {}, "behind": {"source": "nope"}}}], "behind.source")
        self.err([{"at": 0, "shot": {"camera": {}, "behind": "screen"}, "cutout": "frost"}], "already cut out")
        self.err([{"at": 0, "shot": {"screen": None, "behind": "screen"}}], "unknown key 'behind'")

    def test_speaker_over_an_image(self):
        p = self.p([{"at": 0, "shot": {"camera": {}, "behind": {"source": "page", "scroll": True}}, "bubble": False}],
                   "vertical")
        ls = [l for l in frame_at(p, 2)["layers"] if l["type"] in ("image", "video")]
        self.assertEqual([l["type"] for l in ls], ["image", "video"])      # the page, then the speaker over it
        img, cam = ls
        self.assertEqual(img["dst"], [0.0, 0.0, 1080.0, 1920.0])
        self.assertEqual(cam["dst"], [0.0, 0.0, 1080.0, 1920.0])
        self.assertTrue(cam["src"].startswith("main/camera-none-"))       # the plain camera, on the matte's frames
        self.assertTrue(cam["matte"].startswith("main/matte-"))
        self.assertIn(cam["matte"], p["streams"])
        d = p["derive"][cam["src"]]
        self.assertEqual(d["style"], "none")
        self.assertEqual(d["matte"], p["streams"][cam["matte"]]["file"])
        self.assertEqual(p["streams"][cam["matte"]]["offset"], p["streams"][cam["src"]]["offset"])
        self.assertLess(img["rect"][1], [l for l in frame_at(p, 8)["layers"] if l["type"] == "image"][0]["rect"][1])

    def test_over_the_screen_in_a_split_pane(self):
        p = self.p([{"at": 0, "shot": {"split": [{"window": {"title": "left"}}, {"camera": {}, "behind": "screen"}]}}])
        vids = [l for l in frame_at(p, 2)["layers"] if l["type"] == "video" and not l.get("bg")]
        self.assertEqual([("matte" in l, l["src"].split("/")[1][:6]) for l in vids],
                         [(False, "screen"), (False, "screen"), (True, "camera")])
        x, y, w, h = vids[2]["dst"]                                        # the speaker fills the pane;
        bx, by, bw, bh = vids[1]["dst"]                                    # the screen keeps its own fit inside it
        self.assertTrue(x <= bx and y <= by and bx + bw <= x + w + 0.1 and by + bh <= y + h + 0.1)
        self.assertFalse(p["warnings"])

    def test_bubble_hides_and_old_camera_shots_are_untouched(self):
        p = self.p([{"at": 0, "shot": {"camera": {}, "behind": "screen"}}])
        self.assertEqual(len([l for l in frame_at(p, 2)["layers"] if l.get("shape") == "circle"]), 0)
        plain = self.p([{"at": 0, "shot": "camera"}])
        self.assertNotIn("derive", plain)
        self.assertFalse([l for f in plain["frames"] for l in f["layers"] if "matte" in l])


if __name__ == "__main__":
    unittest.main()
