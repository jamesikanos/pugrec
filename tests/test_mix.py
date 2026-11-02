"""audio.music / audio.sfx / audio.loudness: a music bed ducked under speech, sound effects, final loudness."""

import os
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_edit import add_transcript, make_session, plan  # noqa: E402
from pugrec.edit import compose, resolve as R, script as S  # noqa: E402

SCENES = [{"at": 0, "shot": "screen", "bubble": False}]


def tone(path, secs, freq=440, vol=0.1, channels=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=f={freq}:d={secs}:r=48000,volume={vol},aformat=channel_layouts={'stereo' if channels == 2 else 'mono'}",
                    path], check=True)


def render(p, out):
    """The build's audio graph alone -> a 48 kHz mono float array."""
    inputs, graph = compose.audio_args(p, p["frames"], 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[aout]",
                    "-ac", "1", "-ar", "48000", "-f", "f32le", out], check=True)
    return np.fromfile(out, np.float32)


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


class Mix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.d = cls.tmp.name
        make_session(d)
        add_transcript(d)   # speech 1.0-2.5 s and 5.5-7.0 s
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "10",
                        os.path.join(d, "mic-clean.flac")], check=True)    # a silent voice: only the mix is heard
        tone(os.path.join(d, "bed.wav"), 3.0)                                # shorter than the edit: loops
        tone(os.path.join(d, "ding.wav"), 0.2, freq=1500, vol=0.5)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def p(self, audio, output=None, fps=10, **extra):
        return plan(dict({"scenes": SCENES, "audio": audio}, **extra), self.d, output, fps=fps)

    def err(self, data, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse(dict({"scenes": SCENES}, **data), self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation(self):
        self.err({"audio": {"music": {"file": "nope.mp3"}}}, "no such file")
        self.err({"audio": {"music": {"file": "bed.wav", "duck": -3}}}, "audio.music.duck")
        self.err({"audio": {"music": {"file": "bed.wav", "loop": "yes"}}}, "true or false")
        self.err({"audio": {"music": {"volume": -20}}}, "missing 'file'")
        self.err({"audio": {"sfx": [{"at": 1}]}}, "missing 'file'")
        self.err({"audio": {"sfx": [{"at": 1, "file": "bed.wav", "source": "x"}]}}, "unknown source")
        self.err({"audio": {"loudness": -2}}, "audio.loudness")
        open(os.path.join(self.d, "notes.txt"), "w").close()
        self.err({"audio": {"music": "notes.txt"}}, "an audio file")

    def test_no_mix_no_key(self):
        p = self.p({})
        self.assertNotIn("mix", p)
        self.assertNotIn("music", S.parse({"scenes": SCENES}, self.d)["audio"])

    def test_music_curve_ducks_under_speech_and_fades(self):
        p = self.p({"music": {"file": "bed.wav", "volume": -20, "duck": 10, "fade_in": 0.5, "fade_out": 1}})
        m = p["mix"]["music"]
        g = lambda t: float(np.interp(t, *zip(*m["gain"])))
        self.assertEqual(g(0), 0.0)                                  # fades in from silence
        quiet, speech = g(4.0), g(2.0)
        self.assertAlmostEqual(20 * np.log10(speech / quiet), -10, delta=0.2)   # ducked 10 dB under speech
        self.assertAlmostEqual(g(10.0), 0.0, places=3)               # faded out at the end
        self.assertLess(len(m["gain"]), 40)                          # keyframes, not one per frame
        info = R.audio_info(os.path.join(self.d, "bed.wav"))
        self.assertAlmostEqual(20 * np.log10(quiet) + info["lufs"], R.VOICE_LUFS - 20, delta=0.05)

    def test_build_graph_plays_the_curve(self):
        p = self.p({"music": {"file": "bed.wav", "volume": -20, "duck": 10, "fade_in": 0.1, "fade_out": 0.1}}, fps=30)
        x = render(p, os.path.join(self.d, "m.f32"))
        self.assertAlmostEqual(len(x) / 48000, 10.0, delta=0.05)
        sec = lambda a, b: x[int(a * 48000):int(b * 48000)]
        self.assertAlmostEqual(rms_db(sec(1.5, 2.3)) - rms_db(sec(3.5, 4.5)), -10, delta=0.7)
        self.assertGreater(rms_db(sec(8.0, 8.8)), rms_db(sec(3.5, 4.5)) - 1)   # looped: still playing at 8 s

    def test_sfx_fires_when_the_source_reaches_it(self):
        p = self.p({"sfx": [{"at": 4, "file": "ding.wav", "volume": -6}]}, cuts={"ranges": [[2, 3]]}, fps=30)
        fx = p["mix"]["sfx"][0]
        self.assertAlmostEqual(fx["t"], 3.0, places=2)               # source 4 s = output 3 s after the cut
        self.assertAlmostEqual(fx["gain"], 10 ** (-6 / 20), places=4)
        x = render(p, os.path.join(self.d, "s.f32"))
        onset = np.argmax(np.abs(x) > 0.01) / 48000            # sine= peaks at 1/8: 0.03 at -6 dB
        self.assertAlmostEqual(onset, 3.0, delta=0.01)

    def test_sfx_shorthand_on_callouts_and_overlays_and_a_cut_one_warns(self):
        p = self.p({"sfx": [{"at": 2.2, "file": "ding.wav"}]}, cuts={"ranges": [[2, 3]]},
                   callouts=[{"at": 5, "title": "Hi", "sfx": "ding.wav"}])
        self.assertEqual([round(s["t"], 2) for s in p["mix"]["sfx"]], [4.0])
        self.assertTrue([w for w in p["warnings"] if "audio.sfx[0] at 2.2s never plays" in w])
        sc = S.parse({"scenes": SCENES, "callouts": [{"at": 5, "title": "Hi", "sfx": "ding.wav"}]}, self.d)
        self.assertNotIn("sfx", sc["callouts"][0])                   # the callout itself is as before

    def test_music_keeps_running_through_a_hold_and_cards(self):
        p = self.p({"music": {"file": "bed.wav", "duck": 0, "fade_in": 0, "fade_out": 0}},
                   intro={"title": "Hi", "duration": 1}, callouts=[{"at": 4, "title": "X", "pause": 1}], fps=30)
        x = render(p, os.path.join(self.d, "h.f32"))
        self.assertAlmostEqual(len(x) / 48000, p["duration"], delta=0.05)
        for a in (0.2, 5.2):                                         # under the intro, during the hold
            self.assertGreater(rms_db(x[int(a * 48000):int((a + 0.5) * 48000)]), -60)

    def test_loudness_lands_within_half_a_lu(self):
        p = self.p({"music": {"file": "bed.wav", "duck": 0, "fade_in": 0, "fade_out": 0}, "loudness": -14}, fps=30)
        inputs, graph = compose.audio_args(p, p["frames"], 0)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *inputs, "-filter_complex", graph + ";[aout]ebur128[e]",
                            "-map", "[e]", "-f", "null", "-"], capture_output=True, text=True)
        lufs = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])
        self.assertAlmostEqual(lufs, -14, delta=0.5)


if __name__ == "__main__":
    unittest.main()
