"""Record one session: screen (+ mic) via wf-recorder, and a timeline of what happened.

Session folder ~/Videos/PugRec/<stamp>/:
  screen.mkv     AV1 (GPU, VAAPI) at native resolution, VFR, with the mic track
  camera.mkv     webcam, AV1, 1080p30 by default (its first frame's t is in its stream_start)
  session.jsonl  append-only timeline (see FORMAT.md)

Time base: t = ns since the screen video's first frame (its PTS 0). wf-recorder
logs that frame ("... pts_time: 0") as it arrives; that moment, in
CLOCK_MONOTONIC, is the anchor every event is measured from.

Why wf-recorder and not the ScreenCast portal + GStreamer: the portal on PugPal
is capped at 30 fps with SHM copies (the Slack workaround in xdph.conf) and
asks with a picker; wf-recorder captures 60 fps straight from the compositor.
"""

import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from . import hypr, jsonl

FORMAT_VERSION = 1
CURSOR_HZ = 120
WINDOWS_HZ = 10
REDACT_RE = re.compile(r"1password|pinentry|polkit|gcr-prompter|keyring|seahorse|"
                       r"com\.apple\.(Passwords|keychainaccess|SecurityAgent|loginwindow)|coreautha|bitwarden", re.I)
LOCK_LAYER_RE = re.compile(r"lock", re.I)
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_helper.py")
OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay", "countdown.qml")
OVERLAY_HARD_LIMIT_S = 130  # the overlay's own limit is 120 s; then it's killed, whatever its state
MODIFIERS = {"KEY_LEFTSHIFT": "shift", "KEY_RIGHTSHIFT": "shift", "KEY_LEFTCTRL": "ctrl",
             "KEY_RIGHTCTRL": "ctrl", "KEY_LEFTALT": "alt", "KEY_RIGHTALT": "alt",
             "KEY_LEFTMETA": "super", "KEY_RIGHTMETA": "super"}


def videos_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Movies/PugRec")
    try:
        d = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        d = ""
    return os.path.join(d or os.path.expanduser("~/Videos"), "PugRec")


def default_source():
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True,
                              text=True, timeout=3).stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def notify(summary, body="", urgency="normal", replace=None, expire_ms=None):
    """notify-send; returns the notification id (pass it as `replace` to update it in place).
    macOS: a Notification Center banner (no ids: nothing to replace)."""
    if sys.platform == "darwin":
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        try:
            subprocess.run(["osascript", "-e", f"display notification {q(body)} with title {q(summary)} "
                            f"subtitle \"pugrec\""], capture_output=True, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None
    cmd = ["notify-send", "-a", "pugrec", "-u", urgency, "-i", "media-record", "--print-id"]
    if replace:
        cmd += ["-r", str(replace)]
    if expire_ms:
        cmd += ["-t", str(expire_ms)]
    try:
        r = subprocess.run(cmd + [summary, body], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def config():
    """~/.config/pugrec/config.toml: per-machine defaults for record options, e.g.
        mic = "output.podmic_raw"     # main-pc: the unprocessed PodMic; cleaned up afterwards
        keys = true                   # always record key presses (asks for the password each time)
    Command-line flags win."""
    import tomllib
    p = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "pugrec", "config.toml")
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def camera_holders(dev):
    """Names (and pids) of processes with the camera's /dev/videoN open - read-only /proc scan."""
    try:
        target = os.path.realpath(dev)
    except OSError:
        return []
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue  # gone, or not ours to look at
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
                    name = open(f"/proc/{pid}/comm").read().strip()
                    out.append(f"{name} (pid {pid})")
                    break
            except OSError:
                continue
    return out


CAMERA_NAME = "BRIO"  # the recording camera, by name (config: camera = "<path or name regex>")
# The camera's own zoom/pan/tilt for a recording: wide and centred - pugrec frames the face itself.
# Whatever was set before (e.g. a meeting setup) is put back when the recording ends.
# config: [camera_controls] to change them, or camera_controls = false to leave the camera alone.
CAMERA_CONTROLS = {"zoom_absolute": 100, "pan_absolute": 0, "tilt_absolute": 0}


def _v4l_sys(dev):
    return os.path.join("/sys/class/video4linux", os.path.basename(os.path.realpath(dev)))


def camera_is_virtual(dev):
    """A v4l2loopback (or other software) camera: no hardware under it in sysfs."""
    return "/virtual/" in os.path.realpath(_v4l_sys(dev))


def camera_name(dev):
    try:
        with open(os.path.join(_v4l_sys(dev), "name")) as f:
            return f.read().strip()
    except OSError:
        return ""


def default_camera():
    """The camera to record, by stable id (/dev/videoN numbers move around): the config's `camera`
    (a path, or a regex on the device name), else the BRIO, else the first real camera - never a
    virtual one (a v4l2loopback meeting camera may be the desktop's default: it's processed,
    and it's not the camera)."""
    import re
    want = config().get("camera")
    if want and os.path.exists(os.path.expanduser(want)):
        return os.path.expanduser(want)
    d = "/dev/v4l/by-id"
    try:
        nodes = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith("-video-index0")]
    except FileNotFoundError:
        nodes = []
    real = [n for n in nodes if not camera_is_virtual(n)]
    pattern = re.compile(want or CAMERA_NAME, re.I)
    named = [n for n in real if pattern.search(camera_name(n)) or pattern.search(os.path.basename(n))]
    return (named or real or [None])[0]


def v4l2_get(dev, names):
    """{control: value} for the ones the device has (v4l2-ctl), {} if it can't be read."""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", dev, f"--get-ctrl={','.join(names)}"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    vals = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k.strip() in names and v.strip().lstrip("-").isdigit():
            vals[k.strip()] = int(v)
    return vals


def v4l2_set(dev, values):
    if not values:
        return True
    try:
        return subprocess.run(["v4l2-ctl", "-d", dev, "--set-ctrl=" + ",".join(f"{k}={v}" for k, v in values.items())],
                              capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Session:
    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.anchor = None  # CLOCK_MONOTONIC ns of the video's PTS 0
        self.dir = os.path.join(args.dir or videos_dir(), datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.out = jsonl.Writer(os.path.join(self.dir, "session.jsonl"))
        self.focus = {"class": "", "title": ""}
        self.lock_layers = set()
        self.locked = False
        self.mods = set()
        self.threads = []
        self.helper = None
        self.cam_ready = threading.Event()    # camera's first frame arrived (or no camera)
        self.input_ready = threading.Event()  # key helper running (or definitely unavailable)
        self.clock = threading.Event()        # the anchor is set: events can be timed
        self.pending = []                     # notices from before the anchor, written once it's set
        self.wf = None
        self.cam = None
        self.mic_mon = None
        self.last_cursor_move = None  # monotonic ns of the last cursor movement (stall check)
        self.overlay = None
        self.overlay_started = 0.0
        self.ready_file = None
        self.cancelled = False
        self.monitor = ""
        self.counts = {}

    # -- time ------------------------------------------------------------
    def t(self, mono_ns=None):
        return (time.monotonic_ns() if mono_ns is None else mono_ns) - self.anchor

    def emit(self, rec, mono_ns=None):
        rec = {"t": self.t(mono_ns), **rec}
        self.counts[rec["type"]] = self.counts.get(rec["type"], 0) + 1
        self.out.write(rec)

    def notice(self, msg):
        print("pugrec:", msg, file=sys.stderr)
        if self.anchor is not None:
            self.emit({"type": "notice", "message": msg})
        else:
            self.pending.append(msg)

    # -- screen ----------------------------------------------------------
    def pick_monitor(self):
        mons = [hypr.monitor_state(m) for m in hypr.j("monitors")]
        if self.args.output:
            mon = next((m for m in mons if m["name"] == self.args.output), None)
            if not mon:
                raise SystemExit(f"no monitor {self.args.output!r} (have: {', '.join(m['name'] for m in mons)})")
        else:
            mon = next((m for m in mons if m["focused"]), mons[0])
        return mon, mons

    def start_screen(self, mon):
        self.screen_file = os.path.join(self.dir, "screen.mkv")
        # -D: every frame, not only when the screen changes: the video then runs right up to the
        # stop (a still screen used to end it early: camera, screen and timeline must all be
        # the same length), and a gap in it can only mean a stall. ~8 Mbit/s on a still 5K screen.
        cmd = ["wf-recorder", "-y", "-D", "-o", mon["name"], "-m", "matroska", "-f", self.screen_file,
               "-r", str(self.args.fps), "-c", "av1_vaapi", "-d", "/dev/dri/renderD128"]
        self.mic = None if self.args.no_mic else (self.args.mic or config().get("mic") or default_source())
        if self.mic:
            # FLAC: lossless, so the clean-up afterwards (pugrec audio) starts from the real signal.
            cmd += [f"--audio={self.mic}", "-C", "flac"]
        self.wf_log = open(os.path.join(self.dir, "wf-recorder.log"), "w", buffering=1)  # line-buffered: survives a crash
        launched = time.monotonic_ns()
        self.wf = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self.wf_log,
                                   stderr=subprocess.PIPE, text=True, bufsize=1,
                                   start_new_session=True,  # our Ctrl+C mustn't reach it before we stop it
                                   preexec_fn=DIE_WITH_PARENT)
        got = threading.Event()
        method = {}

        def drain():  # read stderr forever: a full pipe would stall the recorder
            for line in self.wf.stderr:
                now = time.monotonic_ns()
                self.wf_log.write(line)
                if not got.is_set():
                    if "pts_time: 0" in line:
                        self.anchor, method["m"] = now, "first-frame log line"
                        got.set()
                    elif line.startswith("Output #0"):
                        method["output0"] = now
            got.set()  # wf-recorder exited

        threading.Thread(target=drain, name="wf-stderr", daemon=True).start()
        got.wait(10)
        if self.wf.poll() is not None:
            raise SystemExit(f"wf-recorder failed (exit {self.wf.returncode}); see {self.wf_log.name}")
        if self.anchor is None:  # newer/older wf-recorder without that log line
            self.anchor = method.get("output0") or launched
            method["m"] = "Output #0 log line" if "output0" in method else "process launch"
        return method["m"]

    # -- pollers ---------------------------------------------------------
    def loop(self, hz, fn, name):
        def run():
            period = 1 / hz
            nxt = time.monotonic()
            while not self.stop.is_set():
                try:
                    fn()
                except (OSError, ValueError, KeyError) as e:
                    self.notice(f"{name}: {e}")
                    self.stop.wait(1)
                nxt += period
                self.stop.wait(max(0, nxt - time.monotonic()))
        th = threading.Thread(target=run, name=name, daemon=True)
        th.start()
        self.threads.append(th)

    def start_cursor(self):
        last = [None]

        def poll():
            mono = time.monotonic_ns()
            p = json.loads(hypr.request("j/cursorpos"))
            xy = (p["x"], p["y"])
            if xy != last[0]:
                if last[0] is not None:
                    self.last_cursor_move = mono  # the cursor is drawn into the video: it must have frames here
                last[0] = xy
                self.emit({"type": "cursor", "x": xy[0], "y": xy[1]}, mono)
        self.loop(CURSOR_HZ, poll, "cursor")

    def check_video_stall(self):
        """After the screen recorder stopped: the video is VFR (frames only when the screen
        changes), so "no frames for a while" is normal - but the cursor is in the picture, so a
        cursor that kept moving after the last video frame means the encoder stalled
        (2026-10-13_19-20-50: video stopped at 63.8 s of 94). Say so now, not in the edit."""
        if not self.last_cursor_move or self.cancelled:
            return
        from .edit.resolve import last_pts
        v = last_pts(self.screen_file, "v:0")
        moved = self.t(self.last_cursor_move) / 1e9
        if v is None:
            msg = "screen.mkv has no readable video"
        elif moved > v + 2.0:
            msg = (f"screen video stopped at {v:.1f}s but the cursor kept moving until {moved:.1f}s: "
                   f"about {moved - v:.0f}s have no picture (re-record, or trim before {v:.0f}s)")
        else:
            return
        self.notice(msg)
        notify("Recording has a gap", msg[0].upper() + msg[1:], "critical")

    def start_windows(self, clients, mons):
        state = {"w": clients, "m": {m["name"]: m for m in mons}}

        def poll():
            mono = time.monotonic_ns()
            mons = list(map(hypr.monitor_state, hypr.j("monitors")))  # first: titles depend on what's shown
            new = hypr.blank_offscreen_titles({c["address"]: hypr.window_state(c) for c in hypr.j("clients")}, mons)
            for rec in hypr.diff_windows(state["w"], new):
                self.emit(rec, mono)
            state["w"] = new
            self.locked = any(m["locked"] for m in mons)
            for m in mons:
                old = state["m"].get(m["name"])
                changed = {k: v for k, v in m.items() if not old or old.get(k) != v}
                if changed:
                    self.emit({"type": "monitor", "name": m["name"], **changed}, mono)
                state["m"][m["name"]] = m
        self.loop(WINDOWS_HZ, poll, "windows")

    def start_hypr_events(self):
        def run():
            try:
                for name, data in hypr.events(self.stop):
                    mono = time.monotonic_ns()
                    self.emit({"type": "hypr", "event": name, "data": data}, mono)
                    if name == "activewindow":
                        cls, _, title = data.partition(",")
                        self.focus = {"class": cls, "title": title}
                    elif name == "activewindowv2":
                        self.emit({"type": "window_focus", "address": "0x" + data if data else None}, mono)
                    elif name == "openlayer" and LOCK_LAYER_RE.search(data):
                        self.lock_layers.add(data)
                    elif name == "closelayer":
                        self.lock_layers.discard(data)
            except OSError as e:
                self.notice(f"hyprland events: {e}")
        th = threading.Thread(target=run, name="hypr-events", daemon=True)
        th.start()
        self.threads.append(th)

    # -- keys and clicks -------------------------------------------------
    def redacting(self):
        f = self.focus
        return self.locked or bool(self.lock_layers) or bool(REDACT_RE.search(f["class"]) or REDACT_RE.search(f["title"]))

    def want_keys(self):
        keys = getattr(self.args, "keys", None)
        return bool(config().get("keys", False) if keys is None else keys)

    def start_input(self, privileged=True):
        cmd = ["/usr/bin/python3", "-I", HELPER]
        if privileged:
            cmd = ["pkexec", *cmd]
        try:
            # Own session: Ctrl+C in this terminal must not reach it. Stopped by closing stdin.
            helper = self.helper = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                                    start_new_session=True)
        except FileNotFoundError:
            self.notice("keys/clicks unavailable: pkexec not found")
            self.input_ready.set()
            return

        def run():
            for line in helper.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.pop("kind")
                mono = ev.pop("mono")
                if kind == "hello":
                    self.input_ready.set()  # the password is in; the rest of the recorder starts now
                    self.clock.wait()
                    self.emit({"type": "input_devices", "devices": ev["devices"], "keys": privileged})
                    continue
                if mono < self.anchor:  # typed while the screen recorder was still starting
                    continue
                if kind == "key":
                    mod = MODIFIERS.get(ev["code"])
                    if mod:
                        (self.mods.add if ev["value"] else self.mods.discard)(mod)
                    rec = {"type": "key", "value": ev["value"], "mods": sorted(self.mods)}
                    if self.redacting() and not mod:
                        rec["redacted"] = True
                    else:
                        rec["code"] = ev["code"]
                else:
                    rec = {"type": kind, "code": ev["code"], "value": ev["value"]}
                self.emit(rec, mono)
            err = helper.stderr.read().strip()
            rc = helper.wait()
            if not self.stop.is_set():
                why = {126: "authentication dismissed", 127: "not authorised"}.get(rc, err or f"exit {rc}")
                if privileged and rc in (126, 127):
                    # Without root the desktop ACL still allows some devices (main-pc: the mouse).
                    self.notice(f"keys unavailable ({why}); recording what's readable without root")
                    self.start_input(privileged=False)
                else:
                    self.notice(f"keys/clicks unavailable: input helper stopped ({why})")
                    self.input_ready.set()
        th = threading.Thread(target=run, name="input", daemon=True)
        th.start()
        self.threads.append(th)

    # -- run -------------------------------------------------------------
    # -- camera ----------------------------------------------------------
    def start_camera(self):
        """ffmpeg: camera MJPEG -> CPU decode -> GPU AV1 -> camera.mkv (PTS from 0).
        ffmpeg logs the first frame's kernel timestamp ("start: <s>"): CLOCK_MONOTONIC
        by default (uvcvideo), so it's on the session clock as is. (With -ts abs it's
        converted to wall clock; a value that large is converted back.)
        (GPU MJPEG decoding segfaulted ffmpeg, and -copyts crashed the AV1 encoder.)"""
        dev = self.args.camera or default_camera()
        if not dev or not os.path.exists(dev):
            self.notice("no camera found; recording without")
            self.cam_ready.set()
            return
        self.set_camera_controls(dev)
        w, h = self.args.camera_size.lower().split("x")
        self.cam_file = os.path.join(self.dir, "camera.mkv")
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-y", "-vaapi_device", "/dev/dri/renderD128",
               "-f", "v4l2", "-input_format", "mjpeg", "-video_size", f"{w}x{h}", "-framerate", "30", "-i", dev,
               "-vf", "format=nv12,hwupload", "-c:v", "av1_vaapi", "-f", "matroska", self.cam_file]
        self.cam_log = open(os.path.join(self.dir, "camera.log"), "w", buffering=1)
        wall_minus_mono = time.time_ns() - time.monotonic_ns()
        self.cam = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self.cam_log, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, start_new_session=True, preexec_fn=DIE_WITH_PARENT)

        def drain():
            seen = False
            for line in self.cam.stderr:
                self.cam_log.write(line)
                if not seen and "Device or resource busy" in line:
                    who = camera_holders(dev)
                    msg = "camera is busy" + (f" - in use by {', '.join(who)}" if who else "") + "; recording without it"
                    self.notice(msg)
                    notify("Camera busy", msg[0].upper() + msg[1:], "critical")
                    self.restore_camera_controls()  # someone else's camera (a meeting): hands off at once
                    seen = True  # one warning, and no "start:" line follows
                m = None if seen else re.search(r"\bstart: ([0-9]+\.[0-9]+)", line)
                if m:
                    seen = True
                    first_mono = int(float(m.group(1)) * 1e9)
                    if first_mono > wall_minus_mono // 2:  # converted to wall clock (epoch): undo
                        first_mono -= wall_minus_mono
                    self.cam_ready.set()  # the screen recorder starts now
                    self.clock.wait()     # the camera starts before the clock exists: time it once it does
                    self.cam_first_t = self.t(first_mono)
                    self.emit({"type": "stream_start", "stream": "camera", "file": "camera.mkv",
                               "device": os.path.basename(dev), "pixels": [int(w), int(h)], "fps": 30,
                               "codec": "av1", "first_frame_t": self.t(first_mono),
                               "controls": getattr(self, "cam_controls", {})}, first_mono)
            self.cam_ready.set()  # ffmpeg ended before a frame: don't wait for it
            rc = self.cam.wait()
            if not self.stop.is_set():
                self.notice(f"camera recording stopped (ffmpeg exit {rc}); see camera.log")
        th = threading.Thread(target=drain, name="camera", daemon=True)
        th.start()
        self.threads.append(th)

    def watch_mic(self):
        """Muted = the source delivers exact digital zeros (a live mic never does).
        Judged from the signal, not a mute flag: on main-pc `hotmic` mutes the iD4's
        hidden hardware node, so PodMic RAW's own flag stays "not muted" while it
        records silence. A low-rate parec copy of the source; passive, ~16 kB/s."""
        try:
            self.mic_mon = subprocess.Popen(
                ["parec", f"--device={self.mic}", "--format=s16le", "--channels=1", "--rate=8000",
                 "--raw", "--latency-msec=100"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, preexec_fn=DIE_WITH_PARENT)
        except FileNotFoundError:
            self.notice("parec not found: can't tell if the mic is muted")
            return
        window = 8000 * 2 // 2  # 0.5 s of s16 mono

        def run():
            state = None
            buf = b""
            while not self.stop.is_set():
                b = self.mic_mon.stdout.read(window - len(buf))
                if not b:
                    return
                buf += b
                if len(buf) < window:
                    continue
                muted = buf.count(0) == len(buf)
                buf = b""
                if muted != state:
                    if state is None and muted:
                        self.notice(f"mic {self.mic} is MUTED: the audio track is silence until it's unmuted")
                        notify("Mic is MUTED", "Recording silence - unmute (hotmic / Stream Deck) to be heard",
                               "critical")
                    state = muted
                    self.emit({"type": "mic_mute", "source": self.mic, "muted": muted})
        th = threading.Thread(target=run, name="mic", daemon=True)
        th.start()
        self.threads.append(th)

    def wait_for(self, ev, what, limit):
        """Wait for a start-up step; False if stopped or cancelled on the overlay meanwhile."""
        if not ev.is_set():
            print(f"pugrec: waiting for the {what}…", file=sys.stderr)
        deadline = time.monotonic() + limit
        while not ev.wait(0.1):
            if self.stop.is_set() or self.overlay_cancelled():
                return False
            if time.monotonic() > deadline:
                self.notice(f"{what} not ready after {limit}s; starting anyway")
                break
        return not (self.stop.is_set() or self.overlay_cancelled())

    def warm_up_and_count_down(self, t0):
        """Everything records from the screen's first frame; the demo starts after the
        countdown. The overlay spins until the camera's first frame, then counts 3-2-1, then
        a `marker` record named "start": the editor begins there, so the warm-up and the
        countdown stay in the files but out of the edit. Stopped or cancelled before that =
        nothing worth keeping (self.cancelled)."""
        self.cancelled = True
        if not self.wait_for(self.cam_ready, "camera", 8):
            self.stop.set()
            return
        n = self.args.countdown
        t_count = time.monotonic()
        if n > 0:
            result = self.overlay_countdown()
            if result in ("cancel", "stopped"):
                self.stop.set()
                return
            if result is None:  # no overlay (Quickshell missing/crashed): notifications instead
                nid = None
                for i in range(n, 0, -1):
                    print(f"pugrec: {i}…", file=sys.stderr)
                    nid = notify(str(i), "Recording starts", replace=nid, expire_ms=900)
                    if self.stop.wait(1.0):
                        return
            if self.stop.is_set():
                return
        self.cancelled = False
        self.emit({"type": "marker", "name": "start", "countdown": n,  # countdown_s > countdown = it was paused
                   "warmup": round(t_count - t0, 2), "countdown_s": round(time.monotonic() - t_count, 2)})
        print("pugrec: ● recording", file=sys.stderr)

    def start_overlay(self, n):
        """The overlay (overlay/countdown.qml, Quickshell): a spinner while the recorder
        starts, then 3-2-1 once overlay_countdown() writes "go" to its ready file. Cancel /
        Esc works throughout. Exit code 0 = go, 3 = cancel."""
        self.ready_file = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"pugrec-ready.{os.getpid()}")
        open(self.ready_file, "w").close()
        waiting = "Getting ready…" if self.args.no_camera else "Starting camera…"
        cmd, env = self.overlay_cmd("countdown", n, waiting, self.ready_file)
        try:
            self.overlay = subprocess.Popen(
                cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, preexec_fn=DIE_WITH_PARENT)
        except FileNotFoundError:
            self.overlay = None
            return
        self.overlay_started = time.monotonic()
        print("pugrec: getting ready (Esc/Cancel on the overlay: discard)", file=sys.stderr)

    def overlay_cmd(self, mode, count=0, waiting="", ready_file=None):
        """The overlay's command line and environment (Quickshell here; MacSession overrides)."""
        env = dict(os.environ, PUGREC_MONITOR=self.monitor, PUGREC_WAITING=waiting)
        if mode == "stopping":
            env["PUGREC_MODE"] = "stopping"
        else:
            env.update(PUGREC_COUNT=str(count), PUGREC_READY_FILE=ready_file or "")
        return ["quickshell", "-p", OVERLAY], env

    def overlay_cancelled(self):
        return bool(self.overlay) and self.overlay.poll() == 3

    def overlay_countdown(self):
        """Tell the overlay to count down: "go", "cancel", "stopped", or None if it isn't running."""
        if not self.overlay or self.overlay.poll() is not None:
            return "cancel" if self.overlay_cancelled() else None
        with open(self.ready_file, "w") as f:
            f.write("go\n")
        print("pugrec: countdown on screen (Space/click the circle: pause, Esc/Cancel: discard)", file=sys.stderr)
        # The overlay holds the keyboard (for Esc): never longer than its own 2-minute limit plus
        # a margin, even if it hangs - this process is separate, so it can always end it.
        deadline = self.overlay_started + OVERLAY_HARD_LIMIT_S
        while self.overlay.poll() is None:
            if self.stop.wait(0.1):  # Ctrl+C / pugrec stop during the countdown
                self._end_overlay()
                return "stopped"
            if time.monotonic() > deadline:
                self._end_overlay()
                self.notice("countdown overlay didn't finish in time; killed it and started recording")
                return "go"
        return {0: "go", 3: "cancel"}.get(self.overlay.returncode)

    def _end_overlay(self):
        if not self.overlay or self.overlay.poll() is not None:
            return
        self.overlay.terminate()
        try:
            self.overlay.wait(2)
        except subprocess.TimeoutExpired:
            self.overlay.kill()
            self.overlay.wait(2)

    def set_camera_controls(self, dev):
        """Wide and centred for the recording (the camera's zoom/pan/tilt may have been set for a
        meeting); the previous values are kept to put back afterwards."""
        self.cam_dev, self.cam_restore = dev, {}
        want = config().get("camera_controls", CAMERA_CONTROLS)
        if not want:
            return
        before = v4l2_get(dev, list(want))
        change = {k: int(v) for k, v in want.items() if k in before and before[k] != int(v)}
        if change and v4l2_set(dev, change):
            self.cam_restore = {k: before[k] for k in change}
        self.cam_controls = {k: int(want[k]) for k in want if k in before}

    def restore_camera_controls(self):
        restore, self.cam_restore = getattr(self, "cam_restore", {}), {}
        if restore:
            v4l2_set(self.cam_dev, restore)

    def trim_camera(self, end_ns):
        """ffmpeg reads the camera for a moment (~0.4 s) after its stop signal: cut camera.mkv
        at the stop, so it ends with the screen and the timeline. Stream copy (no re-encode;
        dropping trailing packets can't break the video)."""
        first = getattr(self, "cam_first_t", None)
        path = getattr(self, "cam_file", None)
        if first is None or not path or not os.path.exists(path):
            return
        keep = (end_ns - first) / 1e9  # seconds of camera time up to the stop
        if keep <= 0:
            return
        root, ext = os.path.splitext(path)
        part = root + ".part" + ext
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
                            "-t", f"{keep:.3f}", part], capture_output=True, text=True)
        if r.returncode == 0 and os.path.getsize(part) > 0:
            os.replace(part, path)
        else:
            if os.path.exists(part):
                os.unlink(part)
            self.notice(f"couldn't trim camera.mkv to the stop ({r.stderr.strip()[-120:]}); it runs a little longer")

    def show_saving(self):
        """The overlay in "stopping" mode: a small spinner + "Saving…" while the files are
        finalised. It takes no keyboard and no clicks (fully click-through): nothing to do but
        wait, and the recorder closes it when done (it also closes itself after 2 minutes)."""
        cmd, env = self.overlay_cmd("stopping", waiting="Saving…")
        try:
            self.saving = subprocess.Popen(
                cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, preexec_fn=DIE_WITH_PARENT)
        except FileNotFoundError:
            self.saving = None

    def hide_saving(self):
        s = getattr(self, "saving", None)
        if s and s.poll() is None:
            s.terminate()
            try:
                s.wait(2)
            except subprocess.TimeoutExpired:
                s.kill()

    def _remove_ready_file(self):
        if self.ready_file:
            try:
                os.unlink(self.ready_file)
            except FileNotFoundError:
                pass

    def abandon(self):
        """Stopped or cancelled before the screen recorder started: nothing to keep."""
        self._end_overlay()
        if self.cam and self.cam.poll() is None:  # the camera may already be running
            self.cam.terminate()
            try:
                self.cam.wait(3)
            except subprocess.TimeoutExpired:
                self.cam.kill()
        self.restore_camera_controls()
        if self.helper and self.helper.poll() is None and not self.input_ready.is_set():
            self.helper.terminate()  # still pkexec, waiting for the password: ours to stop; the prompt goes
        self._stop_helper()
        self.out.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        self._remove_ready_file()
        print("pugrec: cancelled, nothing kept", file=sys.stderr)
        notify("Recording cancelled", "Nothing was kept")

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):  # Ctrl+C / pugrec stop work from the first moment
            signal.signal(sig, lambda *_: self.stop.set())
        t0 = time.monotonic()
        # 1. Input first. With --keys that's the password prompt, and nothing else starts until it's
        # typed, so it comes up at once (the screen recorder's first frame used to take up to 10 s).
        # Keys need root (a password); clicks don't on a normal desktop (the mouse is readable
        # through the seat's ACL), so keys are opt-in and most recordings don't prompt at all.
        if self.args.no_input:
            self.input_ready.set()
        else:
            self.start_input(privileged=self.want_keys())
        if not self.wait_for(self.input_ready, "password prompt / key helper", 90):
            return self.abandon()
        # 2. The overlay: a spinner (Cancel works) while everything else starts.
        mon, mons = self.pick_monitor()
        self.monitor = mon["name"]
        if self.args.countdown > 0:
            self.start_overlay(self.args.countdown)
        clients = hypr.blank_offscreen_titles({c["address"]: hypr.window_state(c) for c in hypr.j("clients")}, mons)
        active = hypr.j("activewindow") or {}
        self.focus = {"class": active.get("class", ""), "title": active.get("title", "")}
        self.locked = any(m["locked"] for m in mons)
        if self.stop.is_set() or self.overlay_cancelled():
            return self.abandon()
        # 3. The camera first: the BRIO needs ~1 s to deliver a frame. The screen recorder starts
        # once it has, so camera.mkv and screen.mkv start (and, stopped together, end) together.
        if self.args.no_camera:
            self.cam_ready.set()
        else:
            self.start_camera()
        if not self.wait_for(self.cam_ready, "camera", 8):
            return self.abandon()
        # 4. The screen recorder: its first frame is the session's clock. Then the rest.
        method = self.start_screen(mon)
        w, h = round(mon["width"]), round(mon["height"])
        self.emit({"type": "session_start", "format": FORMAT_VERSION,
                   "started": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                   "host": socket.gethostname(), "monitors": mons, "windows": list(clients.values()),
                   "focus": active.get("address"), "sync": method})
        self.emit({"type": "stream_start", "stream": "screen", "file": "screen.mkv",
                   "monitor": mon["name"], "logical": [mon["x"], mon["y"], w / mon["scale"], h / mon["scale"]],
                   "pixels": [w, h], "scale": mon["scale"], "fps": self.args.fps, "codec": "av1",
                   "audio": self.mic, "cursor": "embedded"})
        for msg in self.pending:
            self.emit({"type": "notice", "message": msg})
        self.pending = []
        self.clock.set()
        if self.mic:
            self.watch_mic()
        self.start_hypr_events()
        self.start_cursor()
        self.start_windows(clients, mons)
        print(f"pugrec: recording {mon['name']} -> {self.dir}  (stop: pugrec stop / Ctrl+C)", file=sys.stderr)
        # 5. 3-2-1 (the camera is already running).
        self.warm_up_and_count_down(t0)
        while not self.stop.is_set():
            if self.wf.poll() is not None:
                self.notice(f"wf-recorder exited on its own (exit {self.wf.returncode})")
                break
            self.stop.wait(0.5)
        self.finish()

    def finish(self):
        self.stop.set()
        end = self.t()
        # 1. Everything stops capturing in the same instant, so screen, camera and timeline end
        # together (the camera used to be signalled only after the screen was finalised: it
        # recorded ~3 s past the stop, and stopping took ~4 s).
        procs = []
        for p, name, fname in ((self.wf, "wf-recorder", "screen.mkv"), (self.cam, "camera ffmpeg", "camera.mkv")):
            if p and p.poll() is None:
                p.send_signal(signal.SIGINT)  # both finalise their files on SIGINT
                procs.append((p, name, fname))
        if self.mic_mon and self.mic_mon.poll() is None:
            self.mic_mon.terminate()
        if self.helper and self.helper.poll() is None and self.helper.stdin:
            try:
                self.helper.stdin.close()  # the root helper's stop signal
            except OSError:
                pass
        self._end_overlay()  # the countdown overlay, if a stop came during it
        if not self.cancelled:
            self.show_saving()
        # 2. ...and they're waited for together.
        deadline = time.monotonic() + 8
        for p, name, fname in procs:
            try:
                p.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(2)
                self.notice(f"{name} hung on stop and was killed; {fname} may be incomplete")
        self.restore_camera_controls()
        if not self.cancelled:
            self.trim_camera(end)
        try:
            self.check_video_stall()
        except Exception as e:  # noqa: BLE001 - a check must never cost the recording
            print(f"pugrec: video check failed: {e}", file=sys.stderr)
        try:
            self._stop_helper()
            for th in self.threads:
                th.join(2)
        finally:  # whatever failed above, the timeline gets its end and is flushed
            self.emit({"type": "session_end", "duration": end, "counts": dict(self.counts)})
            self.out.close()
            self.wf_log.close()
        self._remove_ready_file()
        self.hide_saving()
        if self.cancelled:  # stopped/cancelled before the countdown ended: the folder only holds warm-up
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)
            print("pugrec: cancelled, nothing kept", file=sys.stderr)
            notify("Recording cancelled", "Nothing was kept")
            return
        print(f"pugrec: saved {self.dir}", file=sys.stderr)
        notify("Recording saved", self.dir)

    def _stop_helper(self):
        h = self.helper
        if not h or h.poll() is not None:
            return
        # It runs as root: we may not signal it. Closing its stdin is the stop signal.
        h.stdin.close()
        try:
            h.wait(3)
        except subprocess.TimeoutExpired:
            self.notice("input helper didn't exit; it will when this process does (PDEATHSIG)")


def _die_with_parent():
    # If the recorder is killed outright, wf-recorder gets SIGINT and still finalises the file.
    import ctypes
    ctypes.CDLL(None).prctl(1, signal.SIGINT)  # PR_SET_PDEATHSIG


# Linux only (prctl). On macOS the capture helper stops when its stdin closes instead.
DIE_WITH_PARENT = _die_with_parent if sys.platform.startswith("linux") else None


def pidfile():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pugrec.pid")


def _cmdline(pid):
    if os.path.exists("/proc"):
        return open(f"/proc/{pid}/cmdline", "rb").read()
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True)
    if r.returncode:
        raise ProcessLookupError(pid)
    return r.stdout


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid():
    try:
        pid = int(open(pidfile()).read().strip())
        cmdline = _cmdline(pid)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    return pid if b"pugrec" in cmdline else None


def claim_pidfile():
    """Atomically make this process the recorder: False if another one already is. Called
    first thing, so a second Stream Deck/keybind press during the start-up stops this
    recording instead of starting a second one. Link, not O_EXCL, so the file is never
    seen empty."""
    tmp = f"{pidfile()}.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(str(os.getpid()))
    try:
        for _ in range(2):
            try:
                os.link(tmp, pidfile())
                return True
            except FileExistsError:
                if running_pid():
                    return False
                try:  # stale: its recorder died without cleaning up
                    os.unlink(pidfile())
                except FileNotFoundError:
                    pass
        return False
    finally:
        os.unlink(tmp)


def release_pidfile():
    if running_pid() == os.getpid():  # never another recorder's
        os.unlink(pidfile())


def stop():
    pid = running_pid()
    if not pid:
        print("pugrec: not recording", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not _alive(pid):
            return 0
        time.sleep(0.1)
    print("pugrec: recorder still running after 10 s", file=sys.stderr)
    return 1
