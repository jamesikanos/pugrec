"""Camera-only sessions (record --no-screen): no screen stream, the mic in mic.mka - every reader copes."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_edit import SEC, add_transcript, plan  # noqa: E402
from pugrec import timeline  # noqa: E402
from pugrec.edit import draft, resolve as R, script as S  # noqa: E402


def make_camera_only(d, mic=True):
    recs = [{"t": 0, "type": "session_start", "format": 2, "started": "2026-11-02T17:00:00.000+00:00", "host": "h",
             "monitors": [], "windows": [], "focus": None, "sync": "camera first frame", "anchor_mono": 123},
            {"t": 0, "type": "stream_start", "stream": "camera", "file": "camera.mkv", "pixels": [1920, 1080],
             "fps": 30, "codec": "av1", "first_frame_t": 0}]
    if mic:
        recs.append({"t": int(0.04 * SEC), "type": "stream_start", "stream": "mic", "file": "mic.mka",
                     "source": "alsa_input.test", "codec": "flac", "first_sample_t": int(0.04 * SEC)})
        open(os.path.join(d, "mic.mka"), "w").close()
    recs += [{"t": int(1 * SEC), "type": "marker", "name": "start", "countdown": 0},
             {"t": int(10 * SEC), "type": "session_end", "duration": int(10 * SEC), "counts": {}}]
    open(os.path.join(d, "camera.mkv"), "w").close()
    with open(os.path.join(d, "session.jsonl"), "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return d


class CameraOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_camera_only(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeline_and_mic_track(self):
        recs = [json.loads(l) for l in open(os.path.join(self.d, "session.jsonl"))]
        tl = timeline.Timeline(recs)
        self.assertFalse(tl.has_screen)
        self.assertEqual((tl.W, tl.H), (1920, 1080))
        self.assertEqual(tl.duration, 10.0)
        self.assertEqual(timeline.mic_track(recs), "mic.mka")
        screen = {"type": "stream_start", "stream": "screen", "file": "screen.mkv"}
        self.assertEqual(timeline.mic_track([dict(screen, audio="alsa_input.x")]), "screen.mkv")   # as before
        self.assertIsNone(timeline.mic_track([screen]))                                          # --no-mic

    def test_camera_shot_resolves_with_the_mic(self):
        p = plan({"audio": {"source": "raw"}, "scenes": [{"at": 1, "shot": "camera"}]}, self.d)
        self.assertNotIn("main/screen", p["streams"])
        self.assertEqual(p["streams"]["main/camera"]["offset"], 0.0)
        self.assertEqual(p["audio"], [{"source": "main", "file": os.path.join(self.d, "mic.mka")}])
        self.assertAlmostEqual(p["duration"], 9.0)        # from the start marker
        self.assertTrue(all(l["src"] == "main/camera" for f in p["frames"] for l in f["layers"] if l["type"] == "video"))

    def test_screen_shots_are_an_error(self):
        for shot in ("screen", "auto", {"window": "focused"}, {"split": ["camera", "screen"]}):
            with self.subTest(shot=shot), self.assertRaises(S.ScriptError) as cm:
                plan({"scenes": [{"at": 1, "shot": shot}]}, self.d)
            self.assertIn("camera-only", str(cm.exception))

    def test_check_reports_it(self):
        from pugrec.edit import regress
        path = os.path.join(self.d, "edit.yaml")
        with open(path, "w") as f:
            json.dump({"scenes": [{"at": 1, "shot": "screen"}]}, f)
        out = regress.check_output(path)
        self.assertEqual(out["rc"], 1)
        self.assertIn("is camera-only (record --no-screen)", out["text"])

    def test_draft_is_a_camera_scene(self):
        add_transcript(self.d)
        path, n = draft.draft(self.d)
        sc = S.load(path)
        self.assertEqual(n, 1)
        self.assertEqual(sc["scenes"][0]["shot"]["kind"], "camera")
        R.resolve(sc, "main", fps=5)                       # and it resolves

    def test_no_mic(self):
        with tempfile.TemporaryDirectory() as d:
            make_camera_only(d, mic=False)
            p = plan({"scenes": [{"at": 1, "shot": "camera"}]}, d)
            self.assertEqual(p["audio"], [])


class AlignMic(unittest.TestCase):
    """The recorder pads / trims mic.mka at the stop so its sample 0 is the session's t = 0."""

    def run_align(self, first_s, end_s):
        import subprocess
        from pugrec import recorder
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mic.mka")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-ac", "1",
                            "-c:a", "flac", path], check=True)
            notes = []
            stub = type("S", (), {"mic_first_t": int(first_s * SEC), "mic_file": path, "notice": notes.append})()
            recorder.Session.align_mic(stub, int(end_s * SEC))
            out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-"],
                                 capture_output=True, check=True).stdout
            import numpy as np
            x = np.frombuffer(out, np.float32)
            return x, notes

    def test_late_start_is_padded(self):
        import numpy as np
        x, notes = self.run_align(0.12, 2.5)
        self.assertEqual(notes, [])
        self.assertAlmostEqual(np.argmax(np.abs(x) > 0.01) / 48000, 0.12, delta=0.002)   # silence, then the mic
        self.assertAlmostEqual(len(x) / 48000, 2.5, delta=0.01)                         # cut at the stop

    def test_early_start_is_trimmed(self):
        x, notes = self.run_align(-0.5, 10)
        self.assertEqual(notes, [])
        self.assertAlmostEqual(len(x) / 48000, 2.5, delta=0.01)   # the half second before t = 0 is gone


if __name__ == "__main__":
    unittest.main()
