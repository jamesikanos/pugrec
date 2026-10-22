"""Pure-logic tests (no Hyprland, no video).  python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_session  # noqa: E402
from pugrec import hypr, jsonl, recorder  # noqa: E402
from pugrec.render import Camera, Timeline, face_crop_path  # noqa: E402


class JsonlTest(unittest.TestCase):
    def test_roundtrip_sorted_and_truncated_line_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            w = jsonl.Writer(p)
            w.write({"t": 5, "type": "b"})
            w.write({"t": 1, "type": "a"})
            w.close()
            with open(p, "a") as f:
                f.write('{"t": 9, "type": "cra')  # crash mid-line
            self.assertEqual([r["type"] for r in jsonl.read(p)], ["a", "b"])

    def test_writer_appends_to_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            for t in (1, 2):
                w = jsonl.Writer(p)
                w.write({"t": t, "type": "x"})
                w.close()
            self.assertEqual(len(jsonl.read(p)), 2)


class WindowDiffTest(unittest.TestCase):
    def test_open_update_close(self):
        a = dict(fake_session.LEFT)
        b = dict(fake_session.RIGHT)
        moved = dict(a, at=[100, 100])
        evs = hypr.diff_windows({"0xa": a, "0xb": b}, {"0xa": moved, "0xc": dict(b, address="0xc")})
        types = sorted(e["type"] for e in evs)
        self.assertEqual(types, ["window_close", "window_open", "window_update"])
        upd = next(e for e in evs if e["type"] == "window_update")
        self.assertEqual(upd, {"type": "window_update", "address": "0xa", "at": [100, 100]})

    def test_no_change_no_events(self):
        a = dict(fake_session.LEFT)
        self.assertEqual(hypr.diff_windows({"0xa": a}, {"0xa": dict(a)}), [])


class OffscreenTitleTest(unittest.TestCase):
    def test_only_shown_windows_keep_titles(self):
        mons = [{"workspace": 1, "special": -98}, {"workspace": 4, "special": 0}]
        ws = {"0xa": {"title": "Shown", "workspace": 1}, "0xb": {"title": "Scratchpad", "workspace": -98},
              "0xc": {"title": "Other monitor", "workspace": 4}, "0xd": {"title": "Client token", "workspace": 2},
              "0xe": {"title": "Hidden", "workspace": 1, "hidden": True}}
        out = hypr.blank_offscreen_titles(ws, mons)
        self.assertEqual({a: w["title"] for a, w in out.items()},
                         {"0xa": "Shown", "0xb": "Scratchpad", "0xc": "Other monitor", "0xd": "", "0xe": ""})
        self.assertEqual(ws["0xd"]["title"], "Client token")  # input untouched
        self.assertEqual(out["0xd"]["workspace"], 2)          # the window itself is kept


class RedactTest(unittest.TestCase):
    def test_password_windows(self):
        for name in ("1Password", "pinentry-gnome3", "polkit-gnome-authentication-agent-1", "gcr-prompter"):
            self.assertTrue(recorder.REDACT_RE.search(name), name)
        for name in ("kitty", "google-chrome", "code"):
            self.assertFalse(recorder.REDACT_RE.search(name), name)


class CameraTest(unittest.TestCase):
    def setUp(self):
        self.tl = Timeline(list(fake_session.records()))
        self.cam = Camera(self.tl, 16 / 9, zoom=2.0)

    def test_widest_view_has_no_bars(self):
        self.assertEqual(self.cam.max_w, 2160 * 16 / 9)  # 3840 on a 5120x2160 screen

    def test_zooms_in_on_clicks_and_frames_window_otherwise(self):
        # At the click the target is fully zoomed in, around the cursor.
        cx, cy, w = self.cam.target(2.0)
        self.assertAlmostEqual(w, self.cam.min_w)
        self.assertLess(abs(cx - self.tl.cursor_at(2.0)[0]), w / 2)
        # Long after any activity: the focused (left) window, clamped to the widest view.
        cx, cy, w = self.cam.target(9.5)
        self.assertEqual(w, self.cam.max_w)
        self.assertLess(cx, fake_session.W / 2)

    def test_leads_activity(self):
        # 0.3 s before the first click the camera already targets it.
        self.assertAlmostEqual(self.cam.target(1.7)[2], self.cam.min_w)

    def test_path_stays_on_screen(self):
        for cx, cy, w in self.cam.path(30):
            h = w / (16 / 9)
            self.assertGreaterEqual(cx - w / 2, -1e-6)
            self.assertLessEqual(cx + w / 2, fake_session.W + 1e-6)
            self.assertGreaterEqual(cy - h / 2, -1e-6)
            self.assertLessEqual(cy + h / 2, fake_session.H + 1e-6)

    def test_path_is_smooth(self):
        path = self.cam.path(60)
        jumps = [abs(b[0] - a[0]) for a, b in zip(path, path[1:])]
        self.assertLess(max(jumps), 120)  # px per frame at 60 fps


class FaceCropTest(unittest.TestCase):
    A = 16 / 9

    def test_no_track_is_centre_square(self):
        self.assertEqual(face_crop_path(None, 30, 0, 3, self.A), [(self.A / 2, 0.5, 1.0)] * 3)

    def test_follows_off_centre_face(self):
        # Face steadily at 25% of the width: the crop centres on it (as far as the edge allows).
        track = [(t / 10, 0.25, 0.5, 0.3) for t in range(50)]
        cx, cy, side = face_crop_path(track, 30, 0, 120, self.A)[-1]
        self.assertAlmostEqual(side, 0.55)                     # tight: face h 0.3 * 1.7 -> min 0.55
        self.assertAlmostEqual(cx, 0.25 * self.A, places=2)    # on the face

    def test_stays_inside_frame_and_moves_smoothly(self):
        track = [(0, 0.05, 0.5, 0.6), (1, 0.95, 0.5, 0.6)]  # a face that jumps across
        path = face_crop_path(track, 30, 0, 60, self.A)
        for cx, cy, side in path:
            self.assertGreaterEqual(cx - side / 2, -1e-9)
            self.assertLessEqual(cx + side / 2, self.A + 1e-9)
        steps = [abs(b[0] - a[0]) for a, b in zip(path, path[1:])]
        self.assertLess(max(steps), 0.05)  # < 5% of frame height per frame


class SpeechGateTest(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy (the .venv)")
        from pugrec import audio
        self.audio = audio

    def test_segments_pad_and_merge(self):
        hop = 512 / 16000
        probs = [0.0] * 10 + [0.9] * 20 + [0.1] * 5 + [0.9] * 20 + [0.0] * 40  # a 0.16 s pause
        segs = self.audio.speech_segments(probs)
        self.assertEqual(len(segs), 1)                               # short pause merged
        self.assertAlmostEqual(segs[0][0], 10 * hop - self.audio.PAD_PRE, places=6)

    def test_breath_between_sentences_is_lowered(self):
        hop = 512 / 16000
        probs = [0.9] * 30 + [0.1] * 60 + [0.9] * 30                 # ~1.9 s gap: a breath
        segs = self.audio.speech_segments(probs)
        self.assertEqual(len(segs), 2)
        n = int(120 * hop * 48000)
        g = self.audio.gain_curve(n, segs)
        mid = int(60 * hop * 48000)
        self.assertAlmostEqual(float(g[mid]), 10 ** (self.audio.BREATH_DB / 20), places=5)
        self.assertEqual(float(g[int(10 * hop * 48000)]), 1.0)
        self.assertLess(float(abs(g[1:] - g[:-1]).max()), 0.01)     # smooth fades, no clicks

    def _take(self):
        """1 s voice, 0.4 s breath, 1 s voice, 0.4 s "sss", 1 s voice, 0.15 s breath, 1 s voice:
        at the levels of a normalised take (voice ~-14 dBFS, breath ~-35)."""
        import numpy as np
        sr, rng = self.audio.BREATH_SR, np.random.default_rng(1)

        def level(x, db):
            return (x * 10 ** (db / 20) / np.sqrt(np.mean(x ** 2))).astype(np.float32)

        def band(n, lo, hi):
            spec = np.fft.rfft(rng.standard_normal(n))
            f = np.fft.rfftfreq(n, 1 / sr)
            spec[(f < lo) | (f > hi)] = 0
            return np.fft.irfft(spec, n)

        t = np.arange(sr) / sr
        voice = level(sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 12)), -14)
        breath = level(band(int(0.4 * sr), 800, 3500), -35)
        hiss = level(band(int(0.4 * sr), 4500, 7900), -25)
        short = level(band(int(0.15 * sr), 800, 3500), -35)
        return np.concatenate([voice, breath, voice, hiss, voice, short, voice])

    def test_breath_inside_speech_found_consonants_left(self):
        runs = self.audio.breath_runs(*self.audio.breath_features(self._take()))
        self.assertEqual(len(runs), 1)                               # the breath, not the "sss" or the short one
        a, b = runs[0]
        self.assertGreater(a, 0.95)
        self.assertLess(b, 1.45)
        self.assertGreater(b - a, 0.3)

    def test_dip_curve_lowers_runs_smoothly_and_leaves_the_rest(self):
        sr = 48000
        g = self.audio.dip_curve(3 * sr, [(1.0, 1.5)], sr=sr)
        floor = 10 ** (self.audio.BREATH_IN_SPEECH_DB / 20)
        self.assertAlmostEqual(float(g[int(1.25 * sr)]), floor, places=5)
        self.assertEqual(float(g[int(0.999 * sr)]), 1.0)              # nothing touched outside the run
        self.assertEqual(float(g[int(1.501 * sr)]), 1.0)
        self.assertLess(float(abs(g[1:] - g[:-1]).max()), 0.01)     # smooth fades, no clicks
        self.assertTrue(bool((self.audio.dip_curve(sr, [(0.5, 0.51)], sr=sr) == 1).all()))  # too short to fade: skipped


class TranscriptTest(unittest.TestCase):
    def test_tokens_to_words(self):
        from pugrec.transcript import words_from_tokens
        toks = [{"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
                {"text": " Hello", "offsets": {"from": 100, "to": 400}, "t_dtw": 12, "p": 0.9},
                {"text": " demo", "offsets": {"from": 400, "to": 600}, "t_dtw": 45, "p": 0.8},
                {"text": "nstrate", "offsets": {"from": 600, "to": 900}, "t_dtw": 60, "p": 0.7},
                {"text": "[_TT_50]", "offsets": {"from": 900, "to": 900}}]
        ws = words_from_tokens(toks, 1000)
        self.assertEqual([w["w"] for w in ws], ["Hello", "demonstrate"])
        self.assertEqual(ws[0]["t"], 120)          # DTW start (centiseconds -> ms)
        self.assertEqual(ws[0]["end"], 400)        # clipped to the next word's start (450)
        self.assertEqual(ws[1]["end"], 900)
        self.assertEqual(ws[1]["p"], 0.7)          # weakest token

    def test_suggest_cuts(self):
        from pugrec.transcript import suggest_cuts
        S = 1_000_000_000
        seg = {"words": [{"t": 0, "end": 1 * S, "w": "So"},
                         {"t": 4 * S, "end": int(4.3 * S), "w": "um,"},
                         {"t": int(4.5 * S), "end": 5 * S, "w": "this"}]}
        cuts = suggest_cuts([seg])
        self.assertEqual(cuts[0], [1.3, 3.7])          # 3 s pause, 0.3 s kept each side
        self.assertEqual(cuts[1], [3.95, 4.35])        # the filler word


class VideoStallTest(unittest.TestCase):
    def check(self, last_frame_s, last_move_s):
        import argparse
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            s = recorder.Session(argparse.Namespace(dir=d))
            s.anchor = 1_000_000_000
            s.screen_file = os.path.join(d, "screen.mkv")
            s.last_cursor_move = s.anchor + int(last_move_s * 1e9)
            sent = []
            with mock.patch("pugrec.edit.resolve.last_pts", return_value=last_frame_s), \
                    mock.patch.object(recorder, "notify", lambda *a, **k: sent.append(a)):
                s.check_video_stall()
            s.out.close()
            return sent

    def test_cursor_moving_after_the_last_frame_is_a_stall(self):
        sent = self.check(63.8, 94.0)
        self.assertEqual(len(sent), 1)
        self.assertIn("stopped at 63.8s", sent[0][1])

    def test_still_screen_at_the_end_is_fine(self):
        self.assertEqual(self.check(90.0, 91.0), [])     # cursor stopped soon after the last frame


class SetupModelTest(unittest.TestCase):
    def test_download_verify_skip_and_replace(self):
        import hashlib
        from unittest import mock
        from pugrec import setup, transcript
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "model.bin")
            with open(src, "wb") as f:
                f.write(b"pretend whisper model" * 1000)
            sha = hashlib.sha256(open(src, "rb").read()).hexdigest()
            cache = os.path.join(d, "cache")
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache}), \
                    mock.patch.object(transcript, "MODEL_URL", "file://" + src), \
                    mock.patch.object(transcript, "MODEL_SHA256", sha):
                dst = os.path.join(cache, "pugrec", transcript.MODEL)
                setup.model()                                          # downloads + verifies
                self.assertEqual(open(dst, "rb").read(), open(src, "rb").read())
                self.assertFalse(os.path.exists(dst + ".part"))
                mtime = os.path.getmtime(dst)
                setup.model()                                          # present + valid: skipped
                self.assertEqual(os.path.getmtime(dst), mtime)
                with open(dst, "wb") as f:
                    f.write(b"corrupt")
                setup.model()                                          # wrong hash: replaced
                self.assertEqual(open(dst, "rb").read(), open(src, "rb").read())
                with mock.patch.object(transcript, "MODEL_SHA256", "0" * 64):
                    os.unlink(dst)
                    with self.assertRaises(setup.SetupError):          # bad download: refused, no file
                        setup.model()
                    self.assertFalse(os.path.exists(dst) or os.path.exists(dst + ".part"))


class CameraChoiceTest(unittest.TestCase):
    """Always the real camera (the BRIO), never a v4l2loopback meeting camera, whatever the default."""

    def pick(self, nodes, names, virtual, cfg=None):
        from unittest import mock
        by = lambda n: os.path.basename(n)  # noqa: E731
        with mock.patch("os.listdir", return_value=nodes), \
                mock.patch.object(recorder, "camera_is_virtual", lambda n: by(n) in virtual), \
                mock.patch.object(recorder, "camera_name", lambda n: names[by(n)]), \
                mock.patch.object(recorder, "config", return_value=cfg or {}):
            got = recorder.default_camera()
        return got and by(got)

    NODES = ["platform-v4l2loopback-0-video-index0", "usb-046d_Logitech_BRIO_X-video-index0",
             "usb-046d_Logitech_BRIO_X-video-index1", "usb-0c45_Other_Cam-video-index0"]
    NAMES = {"platform-v4l2loopback-0-video-index0": "PugCam", "usb-046d_Logitech_BRIO_X-video-index0": "Logitech BRIO",
             "usb-0c45_Other_Cam-video-index0": "Other Cam"}

    def test_brio_even_when_the_loopback_sorts_first(self):
        self.assertEqual(self.pick(self.NODES, self.NAMES, {"platform-v4l2loopback-0-video-index0"}),
                         "usb-046d_Logitech_BRIO_X-video-index0")

    def test_never_a_virtual_camera(self):
        nodes = ["platform-v4l2loopback-0-video-index0", "usb-0c45_Other_Cam-video-index0"]
        self.assertEqual(self.pick(nodes, self.NAMES, {"platform-v4l2loopback-0-video-index0"}),
                         "usb-0c45_Other_Cam-video-index0")
        self.assertIsNone(self.pick(nodes[:1], self.NAMES, {"platform-v4l2loopback-0-video-index0"}))

    def test_config_name_wins(self):
        self.assertEqual(self.pick(self.NODES, self.NAMES, set(), {"camera": "other"}), "usb-0c45_Other_Cam-video-index0")


class CameraControlsTest(unittest.TestCase):
    def session(self, before, cfg=None):
        from unittest import mock
        s = recorder.Session.__new__(recorder.Session)
        sets = []
        with mock.patch.object(recorder, "v4l2_get", lambda dev, names: {k: v for k, v in before.items() if k in names}), \
                mock.patch.object(recorder, "v4l2_set", lambda dev, vals: sets.append(dict(vals)) or True), \
                mock.patch.object(recorder, "config", return_value=cfg or {}):
            s.set_camera_controls("/dev/video0")
            s.restore_camera_controls()
        return sets

    def test_wide_and_centred_then_put_back(self):
        sets = self.session({"zoom_absolute": 180, "pan_absolute": 3600, "tilt_absolute": 0})
        self.assertEqual(sets[0], {"zoom_absolute": 100, "pan_absolute": 0})       # only what differs
        self.assertEqual(sets[1], {"zoom_absolute": 180, "pan_absolute": 3600})    # the meeting setup, restored

    def test_already_right_nothing_touched(self):
        self.assertEqual(self.session({"zoom_absolute": 100, "pan_absolute": 0, "tilt_absolute": 0}), [])

    def test_config_can_leave_the_camera_alone(self):
        self.assertEqual(self.session({"zoom_absolute": 180}, {"camera_controls": False}), [])


class KeysOptInTest(unittest.TestCase):
    """Keys need a password, so they're opt-in: --keys, or `keys = true` in config.toml."""
    def want(self, flag, cfg):
        import argparse
        from unittest import mock
        s = recorder.Session.__new__(recorder.Session)
        s.args = argparse.Namespace(keys=flag)
        with mock.patch.object(recorder, "config", return_value=cfg):
            return s.want_keys()

    def test_default_is_clicks_only(self):
        self.assertFalse(self.want(None, {}))

    def test_config_and_flags(self):
        self.assertTrue(self.want(None, {"keys": True}))
        self.assertTrue(self.want(True, {}))
        self.assertFalse(self.want(False, {"keys": True}))   # --no-keys wins over the config


class FacesNeededTest(unittest.TestCase):
    """The camera file comes from stream_start: camera.mkv on Linux, camera.mov on macOS."""
    def session(self, d, cam_file):
        with open(os.path.join(d, "session.jsonl"), "w") as f:
            f.write('{"t": 0, "type": "stream_start", "stream": "camera", "file": "%s"}\n' % cam_file)
        open(os.path.join(d, cam_file), "w").close()

    def test_mov_and_mkv(self):
        from pugrec import faces
        for cam in ("camera.mkv", "camera.mov"):
            with tempfile.TemporaryDirectory() as d:
                self.session(d, cam)
                self.assertTrue(faces.needed(d), cam)
                open(os.path.join(d, "faces.jsonl"), "w").close()
                self.assertFalse(faces.needed(d), cam)

    def test_no_camera(self):
        from pugrec import faces
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(faces.needed(d))
            with open(os.path.join(d, "session.jsonl"), "w") as f:
                f.write('{"t": 0, "type": "stream_start", "stream": "screen", "file": "screen.mov"}\n')
            self.assertFalse(faces.needed(d))


class MacSessionTest(unittest.TestCase):
    """MacSession's handling of pugrec-capture's lines, without a Mac: what it writes to
    session.jsonl for each kind of event."""
    MON = {"name": "Built-in", "id": 1, "x": 0, "y": 0, "width": 3024, "height": 1964, "scale": 2,
           "workspace": 1, "special": 0, "focused": True, "locked": False}

    def setUp(self):
        import argparse
        from pugrec.recorder_mac import MacSession
        self.tmp = tempfile.TemporaryDirectory()
        self.s = MacSession(argparse.Namespace(dir=self.tmp.name))
        self.s.out.close()
        self.out = []
        self.s.out = type("Out", (), {"write": lambda _, rec: self.out.append(rec)})()

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, anchor=1000):
        self.s.mons = {self.MON["name"]: self.MON}  # as run() seeds them from `pugrec-capture state`
        self.s.on_capture("screen_start", anchor, {"pixels": [3024, 1964], "mic": "MacBook Pro Microphone"})
        self.s.clock.set()

    def windows(self, mono, *wins, focus=None, mons=None):
        self.s.on_windows(mono, {"windows": list(wins), "monitors": mons or [self.MON], "focus": focus})

    def types(self):
        return [r["type"] for r in self.out]

    def test_screen_start_sets_the_anchor(self):
        self.start(1000)
        self.assertEqual(self.s.anchor, 1000)
        self.assertEqual(self.s.screen_pixels, [3024, 1964])
        self.assertTrue(self.s.screen_ready.is_set())

    def test_input_before_the_anchor_is_dropped(self):
        self.s.on_capture("button", 500, {"code": "BTN_LEFT", "value": 1})  # no clock yet
        self.start(1000)
        self.s.on_capture("button", 900, {"code": "BTN_LEFT", "value": 1})  # before the first frame
        self.s.on_capture("button", 1500, {"code": "BTN_LEFT", "value": 1})
        self.assertEqual(self.out, [{"t": 500, "type": "button", "code": "BTN_LEFT", "value": 1}])

    def test_first_cursor_is_not_movement(self):
        self.start()
        self.s.on_capture("cursor", 1100, {"x": 10, "y": 20})
        self.assertIsNone(self.s.last_cursor_move)
        self.s.on_capture("cursor", 1200, {"x": 11, "y": 20})
        self.assertEqual(self.s.last_cursor_move, 1200)
        self.assertEqual(self.types(), ["cursor", "cursor"])

    def test_keys_redacted_in_password_windows(self):
        self.start()
        pw = dict(fake_session.LEFT, address="0x1", **{"class": "com.apple.Passwords", "title": "Passwords"})
        self.windows(1100, pw, focus="0x1")
        self.s.on_capture("key", 1200, {"code": "KEY_LEFTSHIFT", "value": 1})
        self.s.on_capture("key", 1300, {"code": "KEY_A", "value": 1})
        keys = [r for r in self.out if r["type"] == "key"]
        self.assertEqual(keys[0]["code"], "KEY_LEFTSHIFT")  # modifiers are kept
        self.assertTrue(keys[1]["redacted"])
        self.assertNotIn("code", keys[1])
        self.assertEqual(keys[1]["mods"], ["shift"])

    def test_keys_recorded_elsewhere(self):
        self.start()
        self.windows(1100, fake_session.LEFT, focus="0xa")
        self.s.on_capture("key", 1200, {"code": "KEY_A", "value": 1})
        key = self.out[-1]
        self.assertEqual((key["code"], key.get("redacted")), ("KEY_A", None))

    def test_focus_and_titles_tracked(self):
        self.start()
        self.windows(1100, fake_session.LEFT, fake_session.RIGHT, focus="0xa")
        self.assertEqual(self.s.focus, {"class": "kitty", "title": "left"})
        self.windows(1200, fake_session.LEFT, dict(fake_session.RIGHT, title="right 2"), focus="0xb")
        self.assertEqual(self.s.focus, {"class": "kitty", "title": "right 2"})
        self.windows(1300, fake_session.LEFT, focus="0xa")
        self.assertEqual(self.types(), ["window_open", "window_open", "window_focus",
                                        "window_update", "window_focus", "window_close", "window_focus"])
        self.assertEqual(self.out[3], {"t": 200, "type": "window_update", "address": "0xb", "title": "right 2"})
        self.windows(1400, fake_session.LEFT, focus="0xa")  # nothing changed: nothing written
        self.assertEqual(len(self.out), 7)

    def test_monitor_focused_only_change_not_written(self):
        self.start()
        n = len(self.out)
        self.windows(1200, mons=[dict(self.MON, focused=False)])  # the cursor moved to another screen
        self.assertEqual(len(self.out), n)
        self.windows(1300, mons=[dict(self.MON, focused=True, locked=True)])
        self.assertEqual(self.out[-1], {"t": 300, "type": "monitor", "name": "Built-in", "focused": True,
                                        "locked": True})
        self.assertTrue(self.s.locked)

    def test_notices_before_the_anchor_wait(self):
        self.s.on_capture("camera_failed", 500, {"error": "no camera"})
        self.assertTrue(self.s.cam_ready.is_set())
        self.assertEqual(self.out, [])
        self.assertIn("camera unavailable", self.s.pending[0])

    def test_mic_mute_warned_once(self):
        from unittest import mock
        self.start()
        with mock.patch("pugrec.recorder_mac.notify") as notify:
            self.s.on_capture("mic_mute", 1100, {"source": "mic", "muted": True})
            self.s.on_capture("mic_mute", 1200, {"source": "mic", "muted": False})
            self.s.on_capture("mic_mute", 1300, {"source": "mic", "muted": True})
        self.assertEqual(notify.call_count, 1)  # only a mic that starts muted is warned about
        self.assertEqual([r["muted"] for r in self.out if r["type"] == "mic_mute"], [True, False, True])


if __name__ == "__main__":
    unittest.main()
