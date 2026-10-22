"""`pugrec preview SESSION`: a local page that plays an edit, live.

The page (web/) draws the resolved plan's layers on a canvas over <video>
proxies, so what you see is exactly what `build` paints. Saving edit.yaml
re-resolves and the page reloads the plan in place (Server-Sent Events) - errors
show on the page instead of a stale preview.

Proxies (`pugrec proxy SESSION`, made on demand): the 5K screen at 2560 wide and
the camera at 960 wide, AV1 on the GPU (macOS: H.264 on the media engine), 30 fps CFR with a keyframe every second
so scrubbing is quick; times equal session/camera times. Served from 127.0.0.1 only.
"""

import errno
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import resolve, script
from ..audio import PAD_TO_ZERO

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PREVIEW_FPS = 30
VAAPI = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]


# ----------------------------------------------------------------- proxies --
def _proxy(src, dst, width):
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    print(f"proxy: {os.path.basename(src)} -> {os.path.relpath(dst, os.path.dirname(os.path.dirname(dst)))}",
          file=sys.stderr)
    base = ["ffmpeg", "-v", "error", "-y"]
    if sys.platform == "darwin":
        # VideoToolbox: hardware HEVC decode, H.264 encode (Apple Silicon has no AV1 encoder).
        subprocess.run(base + ["-hwaccel", "videotoolbox", "-i", src,
                               "-vf", f"fps={PREVIEW_FPS}:start_time=0,scale='min({width},iw)':-2,format=nv12",
                               "-an", "-c:v", "h264_videotoolbox", "-g", str(PREVIEW_FPS), "-b:v", "6M",
                               "-movflags", "+faststart", dst + ".part.mp4"], check=True)
        os.replace(dst + ".part.mp4", dst)
        return
    tail = ["-an", "-c:v", "av1_vaapi", "-g", str(PREVIEW_FPS), "-b:v", "6M", "-movflags", "+faststart", dst + ".part.mp4"]
    gpu = base + VAAPI + ["-i", src, "-vf", f"fps={PREVIEW_FPS}:start_time=0,scale_vaapi=w={width}:h=-2"] + tail
    if subprocess.run(gpu).returncode != 0:  # e.g. no VAAPI: CPU decode + scale, GPU encode
        cpu = base + ["-vaapi_device", "/dev/dri/renderD128", "-i", src,
                      "-vf", f"fps={PREVIEW_FPS}:start_time=0,scale={width}:-2,format=nv12,hwupload"] + tail
        subprocess.run(cpu, check=True)
    os.replace(dst + ".part.mp4", dst)


def make_proxies(session):
    """{stream: (path, size)} for one resolve.Session; audio as FLAC the browser can play."""
    d = os.path.join(session.path, "proxy")
    out = {}
    scr = os.path.join(session.path, session.screen["file"])
    _proxy(scr, os.path.join(d, "screen.mp4"), 2560)
    out["screen"] = os.path.join(d, "screen.mp4")
    if session.camera:
        cam = os.path.join(session.path, session.camera["file"])
        _proxy(cam, os.path.join(d, "camera.mp4"), 960)
        out["camera"] = os.path.join(d, "camera.mp4")
    clean = os.path.join(session.path, "mic-clean.flac")
    if os.path.exists(clean):
        out["audio"] = clean
    elif session.screen.get("audio"):
        raw = os.path.join(d, "audio.flac")
        if not os.path.exists(raw):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", scr, "-map", "0:a:0",
                            "-af", PAD_TO_ZERO, "-ac", "1", raw], check=True)
        out["audio"] = raw
    return out


def probe_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    w, h = r.stdout.strip().split(",")
    return [int(w), int(h)]


# ------------------------------------------------------------------- state --
class State:
    """Watches edit.yaml (and the derived files it reads), keeps the latest plans."""

    def __init__(self, edit_path):
        self.edit_path = os.path.abspath(edit_path)
        self.lock = threading.Lock()
        self.version = 0
        self.plans = {}       # output name -> JSON bytes
        self.error = None
        self.media = {}       # url path -> file path
        self.sizes = {}
        self.mtimes = None
        self.tracking = set()  # sessions whose face track is being made in the background
        self.reload()

    def track_faces(self, sc):
        """Make a missing faces.jsonl (camera framing) in the background, like build does up
        front: the page is usable at once (camera shots centred until then), and the watcher
        re-resolves when the file appears - it's written whole, then swapped in."""
        from .. import faces
        for s in sc["sources"].values():
            path = s["path"]
            if path in self.tracking or not faces.needed(path):
                continue
            self.tracking.add(path)
            print(f"preview: tracking the face in {os.path.basename(path)}/{os.path.basename(faces.camera_file(path))} "
                  "(the camera framing updates when it's done)", file=sys.stderr)

            def run(path=path):
                try:
                    faces.detect(path)
                except Exception as e:  # noqa: BLE001 - the preview keeps working without it
                    print(f"preview: face tracking failed: {e}", file=sys.stderr)
            threading.Thread(target=run, name="faces", daemon=True).start()

    def watched(self):
        files = [self.edit_path]
        try:
            sc = script.load(self.edit_path)
            if sc["style"].get("theme"):  # editing the theme file updates the preview too
                files.append(script.theme_path(sc["style"]["theme"], os.path.dirname(self.edit_path)))
            if sc["style"].get("logo"):
                files.append(sc["style"]["logo"]["file"])
            for s in sc["sources"].values():
                files += [os.path.join(s["path"], f) for f in ("faces.jsonl", "transcript.jsonl", "mic-clean.flac")]
        except Exception:  # noqa: BLE001 - a broken script still gets watched
            pass
        return files

    def stamp(self):
        return tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in self.watched())

    def reload(self):
        self.mtimes = self.stamp()
        try:
            sc = script.load(self.edit_path)
            self.track_faces(sc)
            sessions = resolve.load_sessions(sc)
            for name, sess in sessions.items():
                for kind, path in make_proxies(sess).items():
                    url = f"/media/{name}/{kind}"
                    self.media[url] = path
                    if kind != "audio" and url not in self.sizes:
                        self.sizes[url] = probe_size(path)
            plans = {}
            for out in sc["outputs"]:
                p = resolve.resolve(sc, out["name"], sessions=sessions, fps=PREVIEW_FPS)
                for sid, st in p["streams"].items():
                    src, kind = sid.split("/")
                    url = f"/media/{src}/{kind}"
                    st["url"], st["proxy_size"] = url, self.sizes[url]
                    del st["file"]
                for a in p["audio"]:
                    a["url"] = f"/media/{a['source']}/audio"
                    del a["file"]
                for k, (img_id, info) in enumerate(sorted(p.get("images", {}).items())):
                    url = f"/media/image/{k}{os.path.splitext(info['file'])[1]}"
                    self.media[url] = info["file"]
                    info["url"] = url
                    del info["file"]
                p["edit"] = os.path.relpath(self.edit_path, os.path.expanduser("~"))
                plans[out["name"]] = json.dumps(p, separators=(",", ":")).encode()
            with self.lock:
                self.plans, self.error = plans, None
                self.version += 1
            print(f"preview: resolved {', '.join(plans)} (v{self.version})", file=sys.stderr)
        except (script.ScriptError, KeyError, ValueError, OSError, subprocess.CalledProcessError) as e:
            with self.lock:
                self.error = str(e)
                self.version += 1
            print(f"preview: {e}", file=sys.stderr)

    def watch(self):
        while True:
            time.sleep(0.4)
            try:
                if self.stamp() != self.mtimes:
                    time.sleep(0.15)  # editors write in steps
                    self.reload()
            except OSError:
                pass


# ------------------------------------------------------------------ server --
def handler_for(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_bytes(self, body, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path):
            size = os.path.getsize(path)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
            if m:  # <video> seeks with Range requests
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = size - int(m.group(2))
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(WEB, "index.html"))
            if path == "/player.js":
                return self.send_file(os.path.join(WEB, "player.js"))
            if path == "/status":
                with state.lock:
                    body = json.dumps({"version": state.version, "error": state.error,
                                       "outputs": list(state.plans)}).encode()
                return self.send_bytes(body, "application/json")
            m = re.fullmatch(r"/plan/(\w+)\.json", path)
            if m:
                with state.lock:
                    body = state.plans.get(m.group(1))
                return self.send_bytes(body, "application/json") if body else self.send_bytes(b"{}", "application/json", 404)
            if path in state.media:
                return self.send_file(state.media[path])
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with state.lock:
                            v, err = state.version, state.error
                        if v != seen:
                            seen = v
                            self.wfile.write(f"data: {json.dumps({'version': v, 'error': err})}\n\n".encode())
                            self.wfile.flush()
                        time.sleep(0.25)
                except (BrokenPipeError, ConnectionResetError):
                    return
            self.send_bytes(b"not found", "text/plain", 404)

    return H


def serve(edit_path, port=8765, open_browser=True):
    state = State(edit_path)
    threading.Thread(target=state.watch, daemon=True).start()
    srv = None
    for p in range(port, port + 20):  # the default may be taken (another local test server): next free one
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), handler_for(state))
            port = p
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    if srv is None:
        raise SystemExit(f"preview: ports {port}-{port + 19} are all in use; pick one with --port")
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"preview: {url}  (edit {state.edit_path}; Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
