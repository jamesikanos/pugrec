"""A session's timeline replayed into queryable state, in screen *pixels* and seconds.

What the editor asks: where was the cursor at t, which windows were visible and
where, which one had focus, where did clicks/typing happen. Built once from
session.jsonl (records sorted by t); queries are binary searches.
"""

import bisect
import re


def mic_track(records):
    """The file holding the mic, its sample 0 at t = 0: a camera-only session's mic.mka (the
    `mic` stream, aligned by the recorder at the stop), else screen.mkv's audio. None if none."""
    streams = {r["stream"]: r for r in records if r["type"] == "stream_start"}
    if "mic" in streams:
        return streams["mic"]["file"]
    if streams.get("screen", {}).get("audio"):
        return streams["screen"]["file"]
    return None


class Timeline:
    def __init__(self, records):
        records = sorted(records, key=lambda r: r.get("t", 0))  # stable: equal t keeps file order
        start = next((r for r in records if r["type"] == "session_start"), None)
        screen = next((r for r in records if r["type"] == "stream_start" and r["stream"] == "screen"), None)
        camera = next((r for r in records if r["type"] == "stream_start" and r["stream"] == "camera"), None)
        if not start or not (screen or camera):
            raise SystemExit("not a pugrec session (no session_start / screen or camera stream_start)")
        self.has_screen = screen is not None
        if screen is None:   # camera-only (record --no-screen): no screen space; the camera's size stands in
            screen = {"pixels": camera["pixels"], "logical": [0, 0] + list(camera["pixels"]), "scale": 1,
                      "monitor": None}
        self.W, self.H = screen["pixels"]
        lx, ly, _, _ = screen["logical"]
        s = self.scale = screen["scale"]
        self.monitor = screen["monitor"]
        self.to_px = lambda x, y: ((x - lx) * s, (y - ly) * s)
        end = next((r for r in records if r["type"] == "session_end"), None)
        self.duration = (end["duration"] if end else records[-1]["t"]) / 1e9

        self.cursor = []        # (t, x, y)
        self.activity = []      # (t, x, y, kind)  kind: click | key | wheel
        self.clicks = []        # (t, x, y)
        self.focus = []         # (t, address or None)
        self.focus_rects = []   # (t, (x, y, w, h) or None) - the focused window, if visible
        self.windows = {}       # address -> [(t, state or None)]   None = closed
        self.visible_ws = []    # (t, {workspace ids visible on our monitor})
        windows = {w["address"]: dict(w) for w in start.get("windows", [])}
        mons = {m["name"]: dict(m) for m in start.get("monitors", [])}
        focus = start.get("focus")
        for addr, w in windows.items():
            self.windows[addr] = [(0.0, dict(w))]
        self.focus.append((0.0, focus))
        cur = None
        last_rect = object()
        last_ws = None
        started = False

        def visible_ids():
            m = mons.get(self.monitor) or {}
            return frozenset(i for i in (m.get("workspace"), m.get("special")) if i)

        def focused_rect():
            w = windows.get(focus)
            if not w or w.get("hidden") or not w.get("mapped", True):
                return None
            if w.get("workspace") not in visible_ids():
                return None  # on another workspace/monitor
            return self.rect_px(w)

        for r in records:
            t = r["t"] / 1e9
            typ = r["type"]
            if not started:  # the state at t=0, before any change (was missing until the first record)
                started = True
                last_ws = visible_ids()
                self.visible_ws.append((0.0, last_ws))
                last_rect = focused_rect()
                self.focus_rects.append((0.0, last_rect))
            if typ == "cursor":
                cur = self.to_px(r["x"], r["y"])
                self.cursor.append((t, *cur))
            elif typ == "button" and r.get("value") == 1 and cur:
                self.clicks.append((t, *cur))
                self.activity.append((t, *cur, "click"))
            elif typ == "key" and r.get("value") in (1, 2) and cur:
                self.activity.append((t, *cur, "key"))
            elif typ == "wheel" and cur:
                self.activity.append((t, *cur, "wheel"))
            elif typ == "window_focus":
                focus = r.get("address")
                self.focus.append((t, focus))
            elif typ == "window_open":
                windows[r["address"]] = {k: v for k, v in r.items() if k not in ("t", "type")}
                self.windows.setdefault(r["address"], []).append((t, dict(windows[r["address"]])))
            elif typ == "window_update" and r["address"] in windows:
                windows[r["address"]].update({k: v for k, v in r.items() if k not in ("t", "type")})
                self.windows[r["address"]].append((t, dict(windows[r["address"]])))
            elif typ == "window_close":
                if windows.pop(r["address"], None) is not None:
                    self.windows[r["address"]].append((t, None))
            elif typ == "monitor" and r["name"] in mons:
                mons[r["name"]].update({k: v for k, v in r.items() if k not in ("t", "type")})
            else:
                continue
            ws = visible_ids()
            if ws != last_ws:
                self.visible_ws.append((t, ws))
                last_ws = ws
            rect = focused_rect()
            if rect != last_rect:
                self.focus_rects.append((t, rect))
                last_rect = rect
        if not self.visible_ws:
            self.visible_ws.append((0.0, visible_ids()))
        self.activity_t = [a[0] for a in self.activity]
        self._series_t = {}

    # -- helpers ---------------------------------------------------------
    def rect_px(self, w):
        x, y = self.to_px(*w["at"])
        return (x, y, w["size"][0] * self.scale, w["size"][1] * self.scale)

    @staticmethod
    def _at(series, t):
        """Last entry of a time-sorted series at or before t (binary search)."""
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        return series[lo - 1] if lo else None

    # -- queries ---------------------------------------------------------
    def cursor_at(self, t):
        c = self._at(self.cursor, t)
        return (c[1], c[2]) if c else (self.W / 2, self.H / 2)

    def focus_at(self, t):
        """Rect of the focused window at t if it's visible on our monitor, else None."""
        f = self._at(self.focus_rects, t)
        return f[1] if f else None

    def focused_address(self, t):
        f = self._at(self.focus, t)
        return f[1] if f else None

    def activity_between(self, a, b):
        return self.activity[bisect.bisect_left(self.activity_t, a):bisect.bisect_right(self.activity_t, b)]

    def window_state(self, address, t):
        """The window's state at t (dict), or None if it didn't exist then."""
        series = self.windows.get(address)
        e = self._at(series, t) if series else None
        return e[1] if e else None

    def visible(self, state, t):
        if not state or state.get("hidden") or not state.get("mapped", True):
            return False
        ws = self._at(self.visible_ws, t)
        return state.get("workspace") in (ws[1] if ws else ())

    def windows_at(self, t):
        """[(address, state)] of windows existing at t."""
        out = []
        for addr in self.windows:
            st = self.window_state(addr, t)
            if st:
                out.append((addr, st))
        return out

    def find_window(self, sel, t):
        """Resolve a selector at time t -> (address, state) or None.

        sel: {"address": "0x.."} | {"focused": true} | {"class": regex} and/or {"title": regex}.
        Several matches: prefer visible, then focused, then largest."""
        if sel.get("focused"):
            addr = self.focused_address(t)
            st = self.window_state(addr, t) if addr else None
            return (addr, st) if st else None
        if sel.get("address"):
            st = self.window_state(sel["address"], t)
            return (sel["address"], st) if st else None
        cls = re.compile(sel["class"], re.I) if sel.get("class") else None
        title = re.compile(sel["title"], re.I) if sel.get("title") else None
        cands = [(a, s) for a, s in self.windows_at(t)
                 if (not cls or cls.search(s.get("class", ""))) and (not title or title.search(s.get("title", "")))]
        if not cands:
            return None
        focused = self.focused_address(t)
        return max(cands, key=lambda c: (self.visible(c[1], t), c[0] == focused, c[1]["size"][0] * c[1]["size"][1]))
