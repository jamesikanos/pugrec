"""Camera cut-out: the style maths, the resolver's derived streams, and the file pipeline (with a
stand-in for RVM, so no model download and no heavy CPU in tests)."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_edit import make_session, plan  # noqa: E402
from pugrec import cutout  # noqa: E402
from pugrec.edit import script as S  # noqa: E402


def scene_frame(w=320, h=180):
    """A white disc (the 'speaker') on a dark, striped room, and its matte."""
    yy, xx = np.mgrid[0:h, 0:w]
    room = np.where((xx // 4) % 2 == 0, 40, 70).astype(np.uint8)
    src = np.repeat(room[..., None], 3, 2).copy()
    disc = (xx - w / 2) ** 2 + (yy - h / 2) ** 2 < (h / 3) ** 2
    src[disc] = 250
    return src, disc.astype(np.float32)


class Composite(unittest.TestCase):
    def test_speaker_kept_room_restyled_no_halo(self):
        src, pha = scene_frame(640, 360)
        out = cutout.composite(src, pha, "frost", 0.2, feather=0)
        disc = pha > 0
        self.assertEqual(np.abs(out[disc].astype(int) - 250).max(), 0)        # the speaker untouched
        room = ~disc
        self.assertLess(out[room].std(), src[room].std() / 2)                 # stripes blurred away
        # No halo: the room right next to the white disc stays near the room's own (frosted) level,
        # where a plain blur would pull the white out into it.
        h, w = pha.shape
        ring = np.zeros_like(disc)
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.hypot(xx - w / 2, yy - h / 2)
        ring = (r > h / 3 + 2) & (r < h / 3 + 6)
        far = r > h / 3 + 40
        halo = lambda o: o[ring].astype(float).mean() - o[far].astype(float).mean()
        self.assertLess(abs(halo(cutout.composite(src, pha, "blur", 0.2, feather=0))), 2)   # normalised: none
        plain = cutout._gauss(src.astype(np.float32), 3.3)                  # what a plain blur would do
        self.assertLess(halo(out), 0.75 * halo(plain))   # frost adds a soft glow on purpose (pugcam's), less

    def test_feather_softens_only_inside(self):
        src, pha = scene_frame(640, 360)
        hard = cutout.composite(src, pha, "blur", 0.2, feather=0)
        soft = cutout.composite(src, pha, "blur", 0.2, feather=6)
        outside = pha == 0
        self.assertTrue(np.array_equal(hard[outside], soft[outside]))       # the fade is inside the outline
        self.assertFalse(np.array_equal(hard, soft))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_session(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cutout_scene_uses_a_derived_stream(self):
        p = plan({"scenes": [{"at": 0, "shot": "screen"}, {"at": 4, "shot": "camera", "cutout": "frost"},
                             {"at": 7, "shot": "camera", "bubble": False}]}, self.d)
        (sid, r), = p["derive"].items()
        self.assertTrue(sid.startswith("main/camera-frost-"))
        self.assertEqual((r["from"], r["to"]), (3.5, 7.5))                   # the scene, plus 0.5 s each side
        self.assertEqual(p["streams"][sid], {"file": r["file"], "size": r["size"], "offset": 3.5})
        self.assertEqual((r["style"], r["strength"], r["feather"]), ("frost", 0.2, 6.0))
        srcs = lambda t: {l["src"] for f in p["frames"] if abs(f["t"] - t) < 0.01 for l in f["layers"] if l["type"] == "video"}
        self.assertEqual(srcs(5.0), {sid})                                   # the cut-out scene
        self.assertEqual(srcs(8.0), {"main/camera"})                         # the next camera scene: raw

    def test_split_pane_and_options(self):
        p = plan({"outputs": [{"name": "vertical", "size": "1080x1920"}],
                  "scenes": [{"at": 0, "shot": {"split": ["screen", {"camera": {}, "cutout": {"style": "blur",
                                                                                              "strength": 0.5}}]}}]},
                 self.d)
        (r,) = p["derive"].values()
        self.assertEqual((r["style"], r["strength"]), ("blur", 0.5))

    def test_no_cutout_no_derive(self):
        p = plan({"scenes": [{"at": 0, "shot": "camera"}]}, self.d)
        self.assertNotIn("derive", p)

    def test_validation(self):
        for scene, needle in (({"at": 0, "shot": "screen", "cutout": "frost"}, "needs the camera"),
                              ({"at": 0, "shot": "camera", "cutout": "glass"}, "expected one of frost, blur"),
                              ({"at": 0, "shot": "camera", "cutout": {"style": "frost", "strength": 2}}, "out of range")):
            with self.assertRaises(S.ScriptError) as cm:
                S.parse({"scenes": [scene]}, self.d)
            self.assertIn(needle, str(cm.exception))


class Pipeline(unittest.TestCase):
    """derive(): chunks, matte + styled parts, concat - on a small synthetic camera, RVM stood in."""

    def test_derive_makes_matte_and_styled_stream(self):
        with tempfile.TemporaryDirectory() as d:
            cam = os.path.join(d, "camera.mkv")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=8",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", cam], check=True)
            r = {"camera": cam, "offset": 0.5, "size": [320, 180], "from": 1.0, "to": 7.0,
                 "matte": os.path.join(d, "cutout", "matte-x.mkv"), "style": "frost", "strength": 0.2,
                 "feather": 2, "file": os.path.join(d, "cutout", "frost-x.mkv")}

            class Disc:
                def __init__(self, threads=4):
                    pass

                def __call__(self, rgb8):
                    return scene_frame(rgb8.shape[1], rgb8.shape[0])[1]

            with mock.patch.object(cutout, "Matter", Disc):
                cutout.derive(r, workers=0, progress=False)
            for f in (r["matte"], r["file"]):
                n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                    "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", f],
                                   capture_output=True, text=True).stdout.strip()
                self.assertEqual(int(n), 180, f)                             # 6 s at 30 fps, two chunks joined
            self.assertFalse(os.path.exists(r["file"] + ".parts"))
            os.remove(r["file"])                                             # a restyle reuses the matte
            with mock.patch.object(cutout, "Matter", side_effect=AssertionError("re-matted")):
                cutout.derive(dict(r, strength=0.5), workers=0, progress=False)
            self.assertTrue(os.path.exists(r["file"]))
