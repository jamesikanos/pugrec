"""Record one session on macOS: the same Session, with pugrec-capture (macos/) doing what
wf-recorder, the camera ffmpeg, the evdev root helper and Hyprland IPC do on Linux.

  screen.mov     HEVC (media engine) at native resolution, VFR, with the mic as ALAC (lossless)
  camera.mov     webcam, HEVC, PTS from 0 = its first frame
  session.jsonl  the same timeline (FORMAT.md)

Apple Silicon has no AV1 encoder, hence HEVC. The helper stamps every line with host time
(mach_absolute_time), the clock time.monotonic_ns() reads here, so its frame times are on
the session clock as they are: the anchor is the screen's first frame itself, not a log line.

macOS has no Spaces API: a window is on screen (workspace 1) or not (-1); every monitor
shows workspace 1. Focus = the frontmost app's frontmost window.
"""

import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from . import hypr
from .recorder import FORMAT_VERSION, MODIFIERS, Session, config, notify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(REPO, "macos", ".build", "release", "pugrec-capture")


def capture_bin():
    if not os.access(CAPTURE, os.X_OK):
        raise SystemExit("pugrec: no macos/.build/release/pugrec-capture - run 'pugrec setup' first")
    return CAPTURE


def state(exclude_pid=None):
    """Monitors, windows, focus, cursor and permissions, right now (without exclude_pid's windows)."""
    cmd = [capture_bin(), "state"] + (["--exclude-pid", str(exclude_pid)] if exclude_pid else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise SystemExit(f"pugrec-capture state failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


class MacSession(Session):
    def __init__(self, args):
        super().__init__(args)
        self.screen_ready = threading.Event()
        self.screen_error = None
        self.hello = None
        self.cam_start = None
        self.windows = {}
        self.mons = {}
        self.win_focus = None
        self.mic_muted = None
        self.mic_name = None
        self.monitor_id = None
        self._cursor_seen = False

    # -- the helper ------------------------------------------------------
    def send(self, cmd):
        try:
            self.wf.stdin.write(cmd + "\n")
            self.wf.stdin.flush()
        except (OSError, ValueError):
            pass

    def start_capture(self, mon):
        self.screen_file = os.path.join(self.dir, "screen.mov")
        self.cam_file = os.path.join(self.dir, "camera.mov")
        if self.args.no_mic:
            self.mic = None
        else:
            self.mic = self.args.mic or config().get("mic") or "default"
        if self.args.no_input:
            mode = "none"
        else:
            mode = "keys" if self.want_keys() else "mouse"
        cmd = [capture_bin(), "record", "--display", str(mon["id"]), "--fps", str(self.args.fps),
               "--screen-out", self.screen_file, "--camera-out", self.cam_file,
               "--camera-size", self.args.camera_size, "--input", mode, "--mic", self.mic or "none"]
        if getattr(self.args, "exclude_pid", None):  # the control app: on screen, not in the video
            cmd += ["--exclude-pid", str(self.args.exclude_pid)]
        if self.args.camera or config().get("camera"):
            cmd += ["--camera", self.args.camera or config().get("camera")]
        self.wf_log = open(os.path.join(self.dir, "capture.log"), "w", buffering=1)
        # Own session: Ctrl+C here mustn't reach it before finish() stops it. If this process
        # dies, its stdin closes, and that's the helper's stop signal too.
        self.wf = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.wf_log,
                                   text=True, bufsize=1, start_new_session=True)
        th = threading.Thread(target=self.read_capture, name="capture", daemon=True)
        th.start()
        self.threads.append(th)

    def read_capture(self):
        for line in self.wf.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self.on_capture(ev.pop("kind"), ev.pop("mono"), ev)
            except (KeyError, ValueError, TypeError) as e:
                self.notice(f"capture: bad line ({e}): {line.strip()[:120]}")
        # EOF: the helper exited. Unblock any start-up wait.
        self.input_ready.set()
        self.cam_ready.set()
        self.screen_ready.set()
        if not self.stop.is_set():
            self.notice(f"pugrec-capture exited on its own (exit {self.wf.wait()}); see capture.log")

    def on_capture(self, kind, mono, ev):
        if kind == "hello":
            self.hello = ev
            self.input_ready.set()
            return
        if kind == "camera_start":
            self.cam_start = (mono, ev)
            self.cam_first_t = None  # set once the clock exists (run)
            self.cam_ready.set()
            return
        if kind == "camera_failed":
            self.notice(f"camera unavailable ({ev.get('error')}); recording without it")
            self.cam_ready.set()
            return
        if kind == "screen_start":
            self.anchor = mono
            self.screen_pixels = ev["pixels"]
            self.mic_name = ev.get("mic")
            self.screen_ready.set()
            return
        if kind == "screen_failed":
            self.screen_error = ev.get("error")
            self.screen_ready.set()
            return
        if kind == "notice":
            self.notice(ev["message"])
            return
        if kind in ("stopped", "screen_stopped"):
            return
        # Timeline events: only once the clock exists (input from before the screen's first
        # frame is warm-up, as on Linux).
        if self.anchor is None or mono < self.anchor:
            return
        self.clock.wait()
        if kind == "cursor":
            if self._cursor_seen:  # the cursor is drawn into the video: it must have frames here
                self.last_cursor_move = mono
            self._cursor_seen = True
            self.emit({"type": "cursor", "x": ev["x"], "y": ev["y"]}, mono)
        elif kind == "windows":
            self.on_windows(mono, ev)
        elif kind == "key":
            mod = MODIFIERS.get(ev["code"])
            if mod:
                (self.mods.add if ev["value"] else self.mods.discard)(mod)
            rec = {"type": "key", "value": ev["value"], "mods": sorted(self.mods)}
            if self.redacting() and not mod:
                rec["redacted"] = True
            else:
                rec["code"] = ev["code"]
            self.emit(rec, mono)
        elif kind in ("button", "wheel"):
            self.emit({"type": kind, "code": ev["code"], "value": ev["value"]}, mono)
        elif kind == "mic_mute":
            if self.mic_muted is None and ev["muted"]:
                self.notice(f"mic {ev['source']} is MUTED: the audio track is silence until it's unmuted")
                notify("Mic is MUTED", "Recording silence - unmute to be heard", "critical")
            self.mic_muted = ev["muted"]
            self.emit({"type": "mic_mute", "source": ev["source"], "muted": ev["muted"]}, mono)

    def on_windows(self, mono, ev):
        new = {w["address"]: w for w in ev["windows"]}
        for rec in hypr.diff_windows(self.windows, new):
            self.emit(rec, mono)
        self.windows = new
        for m in ev["monitors"]:
            old = self.mons.get(m["name"])
            changed = {k: v for k, v in m.items() if not old or old.get(k) != v}
            # "focused" follows the cursor here: written at 10 Hz it would be noise
            if changed and set(changed) != {"focused"}:
                self.emit({"type": "monitor", "name": m["name"], **changed}, mono)
            self.mons[m["name"]] = m
        self.locked = any(m["locked"] for m in ev["monitors"])
        if ev.get("focus") != self.win_focus:
            self.win_focus = ev.get("focus")
            self.emit({"type": "window_focus", "address": self.win_focus}, mono)
        w = new.get(self.win_focus) or {}
        self.focus = {"class": w.get("class", ""), "title": w.get("title", "")}

    # -- overlay ---------------------------------------------------------
    def overlay_cmd(self, mode, count=0, waiting="", ready_file=None):
        cmd = [capture_bin(), "overlay", "--mode", mode, "--count", str(count), "--waiting", waiting]
        if self.monitor_id is not None:
            cmd += ["--display", str(self.monitor_id)]
        if ready_file:
            cmd += ["--ready-file", ready_file]
        return cmd, None

    # -- run -------------------------------------------------------------
    def pick_monitor(self, st=None):
        mons = (st or state())["monitors"]
        if self.args.output:
            mon = next((m for m in mons if self.args.output in (m["name"], str(m["id"]))), None)
            if not mon:
                raise SystemExit(f"no monitor {self.args.output!r} (have: {', '.join(m['name'] for m in mons)})")
        else:
            mon = next((m for m in mons if m["focused"]), mons[0])
        return mon, mons

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop.set())
        t0 = time.monotonic()
        st = state(getattr(self.args, "exclude_pid", None))
        if not st["permissions"]["screen"]:
            notify("pugrec needs Screen Recording", "System Settings > Privacy & Security > Screen Recording: "
                   "allow your terminal, then restart it", "critical")
            print("pugrec: allow Screen Recording for this terminal (System Settings > Privacy & Security), "
                  "restart the terminal, then record again", file=sys.stderr)
        mon, mons = self.pick_monitor(st)
        self.monitor, self.monitor_id = mon["name"], mon["id"]
        # 1. The helper: input tap first (no password on macOS: keys need Input Monitoring, once).
        self.start_capture(mon)
        if not self.wait_for(self.input_ready, "capture helper", 15):
            return self.abandon()
        # 2. The overlay: a spinner (Cancel works) while everything else starts.
        if self.args.countdown > 0:
            self.start_overlay(self.args.countdown)
        self.windows = {w["address"]: w for w in st["windows"]}
        self.mons = {m["name"]: m for m in mons}
        self.win_focus = st.get("focus")
        w = self.windows.get(self.win_focus) or {}
        self.focus = {"class": w.get("class", ""), "title": w.get("title", "")}
        self.locked = any(m["locked"] for m in mons)
        if self.stop.is_set() or self.overlay_cancelled():
            return self.abandon()
        # 3. The camera first (it takes a moment to deliver a frame), then the screen.
        if self.args.no_camera:
            self.cam_ready.set()
        else:
            self.send("camera")
        if not self.wait_for(self.cam_ready, "camera", 8):
            return self.abandon()
        self.send("screen")
        if not self.wait_for(self.screen_ready, "screen recorder", 10):
            return self.abandon()
        if self.anchor is None:
            self.abandon()
            raise SystemExit(f"screen capture failed: {self.screen_error or 'no frame'}"
                             " (Screen Recording allowed for this terminal?)")
        # 4. The clock exists: the header, then everything that was waiting for it. The header
        # is pinned to t=0: ScreenCaptureKit stamps a frame ~10 ms before it reaches us, so
        # "now" would put session_start before its own clock origin.
        pw, ph = self.screen_pixels
        self.emit({"type": "session_start", "format": FORMAT_VERSION,
                   "started": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                   "host": socket.gethostname(), "os": "macos", "monitors": mons,
                   "windows": list(self.windows.values()), "focus": self.win_focus, "sync": "first frame (host clock)"},
                  self.anchor)
        self.emit({"type": "stream_start", "stream": "screen", "file": "screen.mov",
                   "monitor": mon["name"], "logical": [mon["x"], mon["y"], pw / mon["scale"], ph / mon["scale"]],
                   "pixels": [pw, ph], "scale": mon["scale"], "fps": self.args.fps, "codec": "hevc",
                   "audio": self.mic_name, "cursor": "embedded"}, self.anchor)
        for msg in self.pending:
            self.emit({"type": "notice", "message": msg}, self.anchor)
        self.pending = []
        if self.hello and self.hello.get("input"):
            self.emit({"type": "input_devices", "devices": self.hello["devices"], "keys": self.hello["keys"]},
                      self.anchor)
        if self.cam_start:
            first, ev = self.cam_start
            self.cam_first_t = self.t(first)
            self.emit({"type": "stream_start", "stream": "camera", "file": "camera.mov",
                       "device": ev["device"], "pixels": ev["pixels"], "fps": 30, "codec": "hevc",
                       "first_frame_t": self.cam_first_t}, first)
        self.clock.set()
        print(f"pugrec: recording {mon['name']} -> {self.dir}  (stop: pugrec stop / Ctrl+C)", file=sys.stderr)
        # 5. 3-2-1 (the camera is already running).
        self.warm_up_and_count_down(t0)
        while not self.stop.is_set():
            if self.wf.poll() is not None:
                self.notice(f"pugrec-capture exited on its own (exit {self.wf.returncode})")
                break
            self.stop.wait(0.5)
        self.finish()

    def abandon(self):
        if self.wf and self.wf.poll() is None:
            self.wf.send_signal(signal.SIGINT)
            try:
                self.wf.wait(5)
            except subprocess.TimeoutExpired:
                self.wf.kill()
        super().abandon()
