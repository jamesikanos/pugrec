"""audio.breaths: breaths between words lowered at build time (the transcript tells breath from consonant)."""

import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_edit import add_transcript, make_session, plan  # noqa: E402
from pugrec import audio as A  # noqa: E402
from pugrec.edit import compose, script as S  # noqa: E402

SR = 48000
SCENES = [{"at": 0, "shot": "screen", "bubble": False}]
BREATH = (4.6, 5.0)     # in the pause before "Now" (5.5): band noise, breath-coloured
HISS = (3.3, 3.6)       # an "s" outside the words: too bright to be a breath
INSIDE = (6.5, 6.8)     # unvoiced, but inside "editor": speech


def band_noise(n, lo, hi, rng):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.sqrt(np.mean(x ** 2))


def voice(path):
    """10 s: a pitched 'voice' over each word (with gaps of silence), plus the noises above."""
    rng = np.random.default_rng(1)
    x = np.zeros(10 * SR)
    t = np.arange(len(x)) / SR
    pitched = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 8))
    for a, b in [(1.0, 2.5), (5.5, 6.5), (6.8, 7.0)]:
        x[int(a * SR):int(b * SR)] = 0.2 * pitched[int(a * SR):int(b * SR)]
    for (a, b), lo, hi in [(BREATH, 500, 2000), (HISS, 5000, 8000), (INSIDE, 500, 2000)]:
        n = int((b - a) * SR)
        x[int(a * SR):int(a * SR) + n] = 0.05 * band_noise(n, lo, hi, rng)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", path],
                   input=x.astype(np.float32).tobytes(), check=True)


def render(p, out):
    inputs, graph = compose.audio_args(p, p["frames"], 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[aout]",
                    "-ac", "1", "-ar", str(SR), "-f", "f32le", out], check=True)
    return np.fromfile(out, np.float32)


def db(x):
    return 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


class Detect(unittest.TestCase):
    """audio.between_word_breaths on hand-made features (20 ms frames)."""

    def feats(self, n, runs):
        rms, voi, cen = np.full(n, -15.0), np.full(n, 0.9), np.full(n, 500.0)
        for a, b, level, c in runs:
            sl = slice(int(a / 0.02), int(b / 0.02))
            rms[sl], voi[sl], cen[sl] = level, 0.1, c
        return rms, voi, cen

    def test_rules(self):
        runs = [(1.0, 1.4, -22, 900),     # outside words: a breath
                (2.0, 2.4, -22, 900),     # inside a word: speech
                (3.0, 3.4, -22, 6000),    # outside, but s/sh-bright
                (4.0, 4.06, -22, 900),    # too short
                (5.0, 5.6, -22, 900),     # a whole word inside: split round it
                (7.0, 7.4, -60, 900)]     # silence: nothing to lower
        words = [(0.5, 0.98), (1.9, 2.5), (5.25, 5.35), (6.0, 6.5)]
        got = A.between_word_breaths(*self.feats(500, runs), words, 0.12)
        self.assertEqual(len(got), 3, got)
        (a, b), (c, d), (e, f) = got
        self.assertAlmostEqual(a, 1.01, delta=0.03)
        self.assertAlmostEqual(b, 1.41, delta=0.03)
        self.assertLessEqual(d, 5.25 - A.BETWEEN_PAD + 1e-6)   # stops short of the word...
        self.assertGreaterEqual(e, 5.35 + A.BETWEEN_PAD - 1e-6)  # ...and starts again after it
        self.assertEqual(A.between_word_breaths(*self.feats(500, runs), words, 0.5), [])   # min

    def test_zero_length_words_dont_split(self):
        # Whisper stamps some words 0 s long: they don't say where the word is.
        got = A.between_word_breaths(*self.feats(200, [(1.0, 1.5, -22, 900)]), [(1.2, 1.2)], 0.12)
        self.assertEqual(len(got), 1)


class Breaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = cls.tmp.name
        make_session(cls.d)
        add_transcript(cls.d)
        voice(os.path.join(cls.d, "mic-clean.flac"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def p(self, audio, fps=30, **extra):
        return plan(dict({"scenes": SCENES, "audio": audio}, **extra), self.d, fps=fps)

    def err(self, audio, needle):
        with self.assertRaises(S.ScriptError) as cm:
            S.parse({"scenes": SCENES, "audio": audio}, self.d)
        self.assertIn(needle, str(cm.exception))

    def test_validation_and_off(self):
        self.err({"breaths": {"lower": 3}}, "audio.breaths.lower")
        self.err({"breaths": {"min": 0}}, "audio.breaths.min")
        self.err({"breaths": {"between": True}}, "unknown key")
        self.err({"breaths": {"between_words": "yes"}}, "true or false")
        self.assertEqual(S.parse({"scenes": SCENES, "audio": {"breaths": True}}, self.d)["audio"]["breaths"],
                         {"lower": -16.0, "min": 0.12})
        for off in (False, {"between_words": False}):
            self.assertNotIn("mix", self.p({"breaths": off}))
        self.assertNotIn("mix", self.p({}))

    def test_the_breath_is_found_not_the_hiss_or_the_word(self):
        v = self.p({"breaths": True})["mix"]["voice"]
        self.assertEqual(len(v["breaths"]), 1, v["breaths"])
        _, a, b = v["breaths"][0]
        self.assertAlmostEqual(a, BREATH[0], delta=0.04)
        self.assertAlmostEqual(b, BREATH[1], delta=0.04)

    def test_build_lowers_it_and_nothing_else(self):
        cuts = {"ranges": [[3.0, 4.0]]}   # the breath plays 1 s earlier in the output
        plain = render(self.p({}, cuts=cuts), os.path.join(self.d, "a.f32"))
        p = self.p({"breaths": {"lower": -16}}, cuts=cuts)
        low = render(p, os.path.join(self.d, "b.f32"))
        self.assertEqual(len(low), len(plain))
        sec = lambda x, a, b: x[int(a * SR):int(b * SR)]
        a, b = BREATH[0] - 1, BREATH[1] - 1
        self.assertAlmostEqual(db(sec(low, a + 0.05, b - 0.05)) - db(sec(plain, a + 0.05, b - 0.05)), -16, delta=0.5)
        for x0, x1 in [(1.0, 2.5), (4.5, 6.0)]:   # the words either side: untouched
            self.assertLess(np.max(np.abs(sec(low, x0, x1) - sec(plain, x0, x1))), 1e-5)

    def test_no_transcript_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            make_session(d)
            p = plan({"scenes": SCENES, "audio": {"breaths": True}}, d)
            self.assertEqual(p["mix"]["voice"]["breaths"], [])
            self.assertTrue([w for w in p["warnings"] if "audio.breaths" in w and "no transcript" in w])

    def test_check_reports_the_count(self):
        import argparse
        import contextlib
        import io
        import json
        from pugrec import __main__ as cli
        path = os.path.join(self.d, "edit.yaml")
        with open(path, "w") as f:
            json.dump({"sources": {"main": {"session": "."}}, "scenes": SCENES, "audio": {"breaths": True}}, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_check(argparse.Namespace(edit=path))
        self.assertIn("breaths: 1 between words lowered (0.4s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
