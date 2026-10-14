"""Hyprland IPC: request socket (.socket.sock) and event socket (.socket2.sock)."""

import json
import os
import socket


def _dir():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        raise RuntimeError("not running under Hyprland (HYPRLAND_INSTANCE_SIGNATURE unset)")
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "hypr", sig)


def request(cmd):
    """Send one IPC command ("j/clients", "j/cursorpos", ...), return the reply text."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.path.join(_dir(), ".socket.sock"))
        s.sendall(cmd.encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks).decode(errors="replace")


def j(cmd):
    return json.loads(request("j/" + cmd))


def events(stop):
    """Yield (name, data) from the event socket until stop (a threading.Event) is set."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.path.join(_dir(), ".socket2.sock"))
        s.settimeout(0.2)
        buf = b""
        while not stop.is_set():
            try:
                b = s.recv(65536)
            except TimeoutError:
                continue
            if not b:
                return
            buf += b
            *lines, buf = buf.split(b"\n")
            for line in lines:
                name, _, data = line.decode(errors="replace").partition(">>")
                yield name, data


# The window fields worth tracking; everything else in j/clients is noise.
def window_state(c):
    return {
        "address": c["address"],
        "class": c.get("class", ""),
        "title": c.get("title", ""),
        "at": c["at"],
        "size": c["size"],
        "workspace": c["workspace"]["id"],
        "floating": c.get("floating", False),
        "fullscreen": c.get("fullscreen", 0),
        "monitor": c.get("monitor"),
        "mapped": c.get("mapped", True),
        "hidden": c.get("hidden", False),
        "pid": c.get("pid"),
    }


def blank_offscreen_titles(windows, monitors):
    """Privacy: no title for a window that isn't on screen (another workspace, a hidden special
    one, hidden): those can carry client names or tokens. The window itself stays, so a shot can
    follow it back; its title returns with it. windows: {address: state}; monitors: [monitor state].
    Returns a new dict."""
    shown = {i for m in monitors for i in (m.get("workspace"), m.get("special")) if i}
    return {a: (w if w.get("workspace") in shown and not w.get("hidden") else dict(w, title=""))
            for a, w in windows.items()}


def diff_windows(old, new):
    """old/new: {address: window_state}. Returns a list of event records (no t)."""
    out = []
    for addr, w in new.items():
        if addr not in old:
            out.append({"type": "window_open", **w})
        else:
            changed = {k: v for k, v in w.items() if old[addr].get(k) != v}
            if changed:
                out.append({"type": "window_update", "address": addr, **changed})
    for addr in old:
        if addr not in new:
            out.append({"type": "window_close", "address": addr})
    return out


def monitor_state(m):
    return {
        "name": m["name"],
        "x": m["x"], "y": m["y"],
        "width": m["width"], "height": m["height"],
        "scale": m["scale"],
        "transform": m.get("transform", 0),
        "workspace": m["activeWorkspace"]["id"],
        "special": (m.get("specialWorkspace") or {}).get("id", 0),
        "focused": m.get("focused", False),
        # The session-lock screen (DMS) isn't a layer or window; this is where it shows.
        "locked": "LOCK" in (m.get("solitaryBlockedBy") or []),
    }
