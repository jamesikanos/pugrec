"""`pugrec draft SESSION`: a starting edit.yaml from what happened in the recording.

Heuristics, meant to be edited (by hand or by Claude, see EDIT.md):
  - camera intro until the first real window activity (if there's a camera)
  - a window scene (followed) for each focus change that lasts >= MIN_DWELL s,
    ignoring popups/launchers/dialogs and tiny windows
  - a cursor zoom for each burst of clicks/typing inside a window scene
  - cuts for long pauses and filler words (if there's a transcript)
  - outputs main (16:9) and vertical (9:16)
The header lists every window (address, class, title) so selectors are easy to change.
"""

import os
import re

from .resolve import Session

MIN_DWELL = 2.0          # s a focus must last to get its own scene
MIN_SCENE = 1.5          # s
INTRO_S = 3.0            # s of camera at the start (if there's a camera)
BURST_GAP = 1.5          # s between activity events in one burst
BURST_MIN = 2            # clicks/scrolls
# Whole class names only: "rofi" unanchored matched inside "...-Profile_6" (Chrome PWAs).
IGNORE_CLASS = re.compile(r"^(|com\.danklinux\.dms|quickshell|pugpal-spotlight.*|.*polkit.*|pinentry.*|rofi|wofi|"
                          r"fuzzel|.*notification.*)$", re.I)
VISIBILITY_LAG = 0.3     # s: workspaces are polled at 10 Hz, focus events arrive first


def _q(s):
    """YAML-safe double-quoted string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _windows(sess):
    out = {}
    for addr, series in sess.tl.windows.items():
        for _t, st in series:
            if st:
                out[addr] = st
    return out


def focus_spans(sess):
    """[(t0, t1, address)] of focus on real, visible windows, short flips dropped."""
    tl, W, H = sess.tl, sess.tl.W, sess.tl.H
    raw = []
    focus = tl.focus + [(sess.duration, None)]
    for (t0, a), (t1, _b) in zip(focus, focus[1:]):
        st = tl.window_state(a, t0) if a else None
        if not st or IGNORE_CLASS.match(st.get("class", "")) or \
                not tl.visible(st, min(t1, t0 + VISIBILITY_LAG)):
            continue
        w, h = st["size"]
        if w * h * tl.scale ** 2 < 0.04 * W * H:
            continue
        raw.append([t0, t1, a])
    # 1) join consecutive stretches of the same window (focus events repeat while you work in it)
    joined = []
    for s in raw:
        if joined and joined[-1][2] == s[2] and s[0] - joined[-1][1] < MIN_DWELL:
            joined[-1][1] = s[1]
        else:
            joined.append(list(s))
    # 2) then drop short flips: they keep showing the previous window
    spans = []
    for s in joined:
        if s[1] - s[0] >= MIN_DWELL or not spans:
            if spans and spans[-1][2] == s[2]:
                spans[-1][1] = s[1]
            else:
                spans.append(s)
        else:
            spans[-1][1] = s[1]
    return [tuple(s) for s in spans]


def bursts(sess, t0, t1):
    # Clicks and scrolling only: key events carry the *cursor* position, not the text
    # caret's, so a typing burst would zoom on wherever the mouse happens to rest.
    acts = [a for a in sess.tl.activity_between(t0, t1) if a[3] in ("click", "wheel")]
    out, cur = [], []
    for a in acts:
        if cur and a[0] - cur[-1][0] > BURST_GAP:
            if len(cur) >= BURST_MIN:
                out.append((cur[0][0], cur[-1][0]))
            cur = []
        cur.append(a)
    if len(cur) >= BURST_MIN:
        out.append((cur[0][0], cur[-1][0]))
    return out


def draft(session_dir):
    sess = Session(session_dir)
    wins = _windows(sess)
    spans = focus_spans(sess)
    scenes = []

    def add(at, shot, label, transition=None, extra=None):
        at = round(max(begin, at), 2)
        if scenes and at - scenes[-1]["at"] < MIN_SCENE:
            if scenes[-1]["label"] == "Intro":
                at = round(scenes[-1]["at"] + MIN_SCENE, 2)  # keep the intro
            else:
                scenes.pop()
        scenes.append({"at": at, "shot": shot, "label": label, "transition": transition, "extra": extra or {}})

    begin = sess.start  # after the recorder's countdown (0 for older recordings)
    intro = begin + (INTRO_S if sess.camera else 0.0)
    if sess.camera:
        add(begin, "camera", "Intro")
    for t0, t1, addr in spans:
        if t1 <= intro:
            continue
        st = wins[addr]
        cls = st["class"]
        # Generated class names (Chrome PWAs: chrome-<id>-Profile_6) say nothing: title only.
        name = (st["title"][:48] if len(cls) > 24 else f"{cls}: {st['title'][:40]}").strip()
        start = max(t0, intro)
        shot = f"{{window: {{address: {_q(addr)}}}, follow: true}}"
        trans = "{fade: 0.4}" if scenes and scenes[-1]["shot"] == "camera" else "{move: 0.6}"
        add(start if scenes else begin, shot, name, trans)
        for b0, b1 in bursts(sess, max(t0, intro), t1):
            add(b0 - 0.5, "{cursor: {zoom: 2.0}}", f"Zoom: {name[:30]}", "{move: 0.6}")
            if b1 + 1.2 < t1 - MIN_SCENE:
                add(b1 + 1.2, shot, name, "{move: 0.6}")
    if not scenes:
        add(begin, "auto", "Whole recording")

    lines = ["# Draft edit, made by `pugrec draft` - edit freely. Reference: EDIT.md in the pugrec repo.",
             "# Check: pugrec check .   Preview: pugrec preview .   Export: pugrec build .", "#",
             "# Windows in this recording (use address, class or title in window: selectors):"]
    for addr, st in wins.items():
        lines.append(f"#   {addr}  {st['class']:<28.28}  {st['title'][:60]}")
    lines += ["", "version: 1", "outputs:", "  - {name: main, size: 1920x1080}", "  - {name: vertical, size: 1080x1920}"]
    start = next((r for r in sess.records if r["type"] == "session_start"), {})
    if spans and start.get("os") == "macos":
        # macOS windows overlap: a fixed window's rectangle would keep whatever is stacked on
        # top of it sharp. The focused window is the frontmost one, so nothing covers it.
        lines += ["style:", "  spotlight: {window: focused}   # whatever has focus is sharp, the rest blurred "
                            "(false = off)"]
    elif spans:  # the window with the most focus time stays sharp; the rest of the screen is blurred
        main = max({a for _, _, a in spans}, key=lambda a: sum(t1 - t0 for t0, t1, b in spans if b == a))
        lines += ["style:", f"  spotlight: {{window: {{address: {_q(main)}}}}}   # {wins[main]['title'][:50]} "
                            "- sharp, the rest blurred (false = off)"]
    if sess.transcript:
        lines += ["cuts:", "  pauses: 1.2        # cut pauses longer than this (s), from transcript.jsonl",
                  "  fillers: true      # and um/uh/erm", "  ranges: []        # [[from, to], ...] by hand"]
    else:
        lines += ["cuts: []             # [[from, to], ...] seconds; `pugrec transcript` enables pauses/fillers"]
    lines.append("scenes:")
    for s in scenes:
        lines.append(f"  - at: {s['at']}")
        lines.append(f"    label: {_q(s['label'])}")
        lines.append(f"    shot: {s['shot']}")
        if s["transition"] and s is not scenes[0]:
            lines.append(f"    transition: {s['transition']}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(session_dir, "edit.yaml")
    if os.path.exists(path):
        path = os.path.join(session_dir, "edit.draft.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, len(scenes)
