"""Load and validate edit.yaml into one normalised shape.

Strict on purpose: a script is written by hand or by Claude, so every unknown
key, wrong type or impossible value is an error naming where it is
(`scenes[3].shot.window: unknown key 'clas' (did you mean 'class'?)`).
Shorthands are expanded here, so the resolver sees one form per concept.
"""

import difflib
import os
import re

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
FITS = ("auto", "cover", "contain", "inset")
BACKGROUNDS = "blur, backdrop or a colour like '#16171a'"
SHOT_KINDS = ("screen", "camera", "auto", "window", "cursor", "rect", "split")
DEFAULT_STYLE = {  # = the PugPal look; a theme (style.theme) or edit.yaml's style overrides any of it
    "theme": None,                     # name in ~/.config/pugrec/themes/, or a path to a .yaml
    "accent": "#ff6b2c",               # FieldPal orange: click rings, caption highlight, card accent
    "bubble": {"shape": "circle", "size": 0.26, "corner": "bottom-left", "margin": 0.04, "show": True,
               "border": {"color": "#f1e4d9", "width": 3}},   # Jesse's cream; width in px at 1080p, 0 = none
    "click_rings": True,
    "logo": None,                      # {file, corner, size, margin, opacity}: a small watermark
    # The sparkle/sweep transitions: star colours (null = white + accent), and the sweep's sprite
    # (an image that crosses the frame revealing the next scene; size = its height / shorter side).
    "sparkle": {"colors": None, "image": None, "image_size": 0.3, "pixelated": False, "flip": False,
                "count": 36},
    "card": {"background": "#16171a", "color": "#f1e4d9", "font": None, "duration": 3.0,   # intro/outro
             "image": None, "image_size": 0.12,    # image: a brand icon above the title; size = height / shorter side
             # seconds; fade_in null = 0 for the intro (frame 0 is the thumbnail on WhatsApp,
             # LinkedIn, YouTube: it must be the whole card), 0.4 for the outro
             "fade_in": None, "fade_out": 0.4,
             "title_case": False},   # true: card titles in Title Case (a brand's house style)
    "background": "#16171a",          # PugPal ink: behind splits / contain without blur
    "blur": 40,                        # px at 1080p for blurred backgrounds
    "radius": 6,                       # px at 1080p: rounded panes (PugPal look)
    "gap": 0.02,                       # split gap, fraction of the output's shorter side
    "padding": 0.04,                   # space around a framed window, fraction of the window
    # fit: inset - the picture as a rounded card with room around it, on the backdrop. Only used
    # by scenes/panes that ask for it (fit: inset / background: backdrop); nothing else changes.
    "backdrop": {"gradient": ["#16171a", "#32231f", "#e8480f"],   # ink -> Jesse's mask -> FieldPal orange
                 "angle": 160,         # CSS degrees: 0 = towards the top, 90 = right, 180 = down
                 "image": None},       # or an image (cover-cropped); setting one clears the other
    "inset": {"padding": 0.06,         # room around the card, fraction of the pane's shorter side
              "radius": 18,            # card corners, px at 1080p
              "aspect": "auto",        # auto (camera: fill the room; others: the target's shape) or "W:H"
              "position": [0.5, 0.5],  # where the card sits in the free space (0 = left/top)
              "shadow": 0},            # 0 = none (the PugPal look) .. 1 = strong
    # Slow push-in while a shot holds (Ken Burns): {to: 0.8, over: 10} = the view shrinks to 80%
    # over 10 s. null = off; scenes say `push: true` (or a theme sets it here).
    "push": None,
    # Punch-in on an important point: a quick ~6% zoom in (with a slight dip out first - the
    # anticipation), held, then eased back out. Only where a scene says `punch: <source s>`.
    "punch": {"zoom": 0.06, "dip": 0.006, "hold": 1.5},
    # One window sharp, the rest of the screen blurred + dimmed: {window: SEL, blur, dim}. Off by default.
    "spotlight": None,
    "transition": {"move": 0.6},       # default between scenes
    # From transcript.jsonl (pugrec transcript); shown only if the source has one.
    # Callouts: small animated info boxes (edit.yaml `callouts:`), not full-frame title cards.
    # look: draw = the border traces itself round the box, then the fill and the text fade in;
    # bar = an accent bar grows down the left edge and the text slides out from behind it;
    # pill = one short label (a role, a name) that fades in; section = a full-frame panel in the
    # card colours wipes across with a big title (a section header, over the running video and
    # audio - unlike intro/outro cards). sparkle: true = the theme's unicorn sweep leads the wipe
    # (only themes with a `sparkle:` section, like the transitions). Sizes are fractions of the output's
    # shorter side; px values are at 1080p. from/to: the direction of travel in and out.
    "callout": {"look": "bar", "corner": "top-left", "margin": 0.045,
                "size": 0.034, "text_size": 0.024, "padding": 0.02,
                "font": None, "weight": 700, "text_weight": 500,   # font null = captions'
                "color": "#f1e4d9", "background": "#16171a", "opacity": 0.86,
                "border": None, "border_width": 2, "radius": 12,    # border null = the accent
                "duration": 4.5, "in": 0.7, "out": 0.35,            # seconds, in and out included
                "from": "auto", "to": "none", "distance": 0.025,    # auto = from its corner's side
                "sparkle": False,
                "pause": 0},   # s: freeze the recording (picture and sound) at `at` while it comes in
    "captions": {"show": True, "size": 0.045, "position": "auto", "max_words": 6, "highlight": True,
                 "fix": {},  # word -> correction, for names the transcript gets wrong (Akme -> Acme)
                 "font": "IBM Plex Sans", "weight": 600, "color": "#f1e4d9",
                 "background": "#16171a", "background_opacity": 0.78},
}
THEMES_HELP = "~/.config/pugrec/themes/<name>.yaml"
CALLOUT_LOOKS = ("draw", "bar", "pill", "section")
CALLOUT_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right", "top", "bottom")
DIRECTIONS = ("left", "right", "up", "down", "none")


class ScriptError(ValueError):
    pass


def _err(path, msg):
    raise ScriptError(f"{path}: {msg}")


def _keys(d, path, allowed, required=()):
    if not isinstance(d, dict):
        _err(path, f"expected a mapping, got {type(d).__name__}")
    for k in d:
        if k not in allowed:
            close = difflib.get_close_matches(str(k), allowed, n=1)
            _err(path, f"unknown key {k!r}" + (f" (did you mean {close[0]!r}?)" if close else
                                               f" (allowed: {', '.join(allowed)})"))
    for k in required:
        if k not in d:
            _err(path, f"missing {k!r}")


def _num(v, path, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _err(path, f"expected a number, got {v!r}")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        _err(path, f"{v} is out of range [{lo}, {hi}]")
    return float(v)


def _rect(v, path):
    if not (isinstance(v, (list, tuple)) and len(v) == 4):
        _err(path, f"expected [x, y, w, h], got {v!r}")
    r = [_num(x, f"{path}[{i}]", 0) for i, x in enumerate(v)]
    if r[2] <= 0 or r[3] <= 0:
        _err(path, "width and height must be > 0")
    return r


def _size(v, path):
    try:
        w, h = (int(x) for x in str(v).lower().split("x"))
    except ValueError:
        _err(path, f"expected WIDTHxHEIGHT like 1920x1080, got {v!r}")
    if w % 2 or h % 2 or w < 16 or h < 16:
        _err(path, "width and height must be even and >= 16 (H.264 4:2:0)")
    return (w, h)


def _corner(v, path):
    if isinstance(v, str):
        if v not in CORNERS:
            _err(path, f"expected one of {', '.join(CORNERS)} or [x, y] (fractions), got {v!r}")
        return v
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return [_num(v[0], f"{path}[0]", 0, 1), _num(v[1], f"{path}[1]", 0, 1)]
    _err(path, f"expected a corner name or [x, y], got {v!r}")


# ------------------------------------------------------------------- shots --
def shot(v, path):
    """Normalise a shot to {"kind": .., ...}."""
    if isinstance(v, str):
        if v not in ("screen", "camera", "auto"):
            close = difflib.get_close_matches(v, SHOT_KINDS, n=1)
            _err(path, f"unknown shot {v!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
        return {"kind": v} if v != "auto" else {"kind": "auto", "zoom": 2.0}
    if not isinstance(v, dict):
        _err(path, f"expected a shot name or mapping, got {v!r}")
    kinds = [k for k in v if k in SHOT_KINDS]
    if len(kinds) != 1:
        if kinds:
            _err(path, f"one shot per scene: got {', '.join(kinds)} (use split: [a, b] for two)")
        close = difflib.get_close_matches(next(iter(v), ""), SHOT_KINDS, n=1)
        _err(path, f"no shot kind (one of {', '.join(SHOT_KINDS)})" + (f"; did you mean {close[0]!r}?" if close else ""))
    kind = kinds[0]
    p = f"{path}.{kind}"
    if kind in ("screen",):
        _keys(v, path, ["screen"])
        return {"kind": "screen"}
    if kind == "camera":
        _keys(v, path, ["camera", "follow"])
        opt = v["camera"] or {}
        _keys(opt, p, ["zoom"])
        out = {"kind": "camera", "zoom": _num(opt.get("zoom", 1.0), f"{p}.zoom", 1, 4)}
        if not v.get("follow", True):
            out["follow"] = False   # only when asked: existing edits keep resolving to the same plan
        return out
    if kind == "auto":
        _keys(v, path, ["auto"])
        opt = v["auto"] or {}
        _keys(opt, p, ["zoom"])
        return {"kind": "auto", "zoom": _num(opt.get("zoom", 2.0), f"{p}.zoom", 1, 8)}
    if kind == "window":
        _keys(v, path, ["window", "follow", "region", "padding"])
        sel = v["window"]
        if sel == "focused":
            sel = {"focused": True}
        _keys(sel, p, ["class", "title", "address", "focused"])
        if not sel:
            _err(p, "give class, title, address or focused: true")
        out = {"kind": "window", "select": dict(sel), "follow": bool(v.get("follow", True))}
        if "region" in v:
            r = _rect(v["region"], f"{path}.region")
            if r[0] + r[2] > 1.0001 or r[1] + r[3] > 1.0001:
                _err(f"{path}.region", "fractions of the window: x + w and y + h must be <= 1")
            out["region"] = r
        if "padding" in v:
            out["padding"] = _num(v["padding"], f"{path}.padding", 0, 1)
        return out
    if kind == "cursor":
        _keys(v, path, ["cursor"])
        opt = v["cursor"] or {}
        _keys(opt, p, ["zoom", "dead_zone"])
        return {"kind": "cursor", "zoom": _num(opt.get("zoom", 2.0), f"{p}.zoom", 1, 8),
                "dead_zone": _num(opt.get("dead_zone", 0.2), f"{p}.dead_zone", 0, 0.45)}
    if kind == "rect":
        _keys(v, path, ["rect"])
        return {"kind": "rect", "rect": _rect(v["rect"], p)}
    if kind == "split":
        _keys(v, path, ["split", "ratio", "layout"])
        panes = v["split"]
        if not isinstance(panes, list) or len(panes) != 2:
            _err(p, "expected exactly two shots: split: [a, b]")
        inner = []
        for i, s in enumerate(panes):
            pp = f"{p}[{i}]"
            opts = {}
            if isinstance(s, dict) and any(k in s for k in PANE_KEYS):
                s = dict(s)
                opts = {k: s.pop(k) for k in PANE_KEYS if k in s}
            sh = shot(s, pp)
            if "fit" in opts:
                if opts["fit"] not in FITS:
                    _err(f"{pp}.fit", f"expected one of {', '.join(FITS)}, got {opts['fit']!r}")
                sh["fit"] = opts["fit"]
            if "background" in opts:
                sh["background"] = _background(opts["background"], f"{pp}.background")
            if "inset" in opts:
                inset(opts["inset"], f"{pp}.inset", DEFAULT_STYLE["inset"])  # validate now
                sh["inset_raw"] = opts["inset"]
            inner.append(sh)
        if any(s["kind"] == "split" for s in inner):
            _err(p, "a split can't contain another split")
        layout = v.get("layout", "auto")
        if layout not in ("auto", "row", "column"):
            _err(f"{path}.layout", f"expected auto, row or column, got {layout!r}")
        return {"kind": "split", "panes": inner, "ratio": _num(v.get("ratio", 0.5), f"{path}.ratio", 0.2, 0.8),
                "layout": layout}


def transition(v, path):
    if v in (None, "cut"):
        return {"type": "cut", "duration": 0.0}
    if isinstance(v, dict):
        _keys(v, path, ["move", "fade", "cut", "sparkle", "sweep"])
        if len(v) != 1:
            _err(path, "one of move: <s>, fade: <s>, sparkle: <s>, sweep: <s> or cut")
        (k, d), = v.items()
        if k == "cut":
            return {"type": "cut", "duration": 0.0}
        return {"type": k, "duration": _num(d, f"{path}.{k}", 0.05, 5)}
    _err(path, f"expected cut, {{move: s}}, {{fade: s}}, {{sparkle: s}} or {{sweep: s}}, got {v!r}")


def bubble(v, path, base):
    """Merge a scene's bubble settings over the style's."""
    if v is None:
        return dict(base)
    if v is False:
        return dict(base, show=False)
    if v is True:
        return dict(base, show=True)
    _keys(v, path, ["corner", "size", "show", "shape", "margin", "border"])
    out = dict(base)
    if "border" in v:
        b = v["border"]
        if b is False:
            out["border"] = {"color": base["border"]["color"], "width": 0}
        else:
            _keys(b, f"{path}.border", ["color", "width"])
            out["border"] = {"color": _color(b.get("color", base["border"]["color"]), f"{path}.border.color"),
                             "width": _num(b.get("width", base["border"]["width"]), f"{path}.border.width", 0, 40)}
    if "corner" in v:
        out["corner"] = _corner(v["corner"], f"{path}.corner")
    if "size" in v:
        out["size"] = _num(v["size"], f"{path}.size", 0.05, 0.6)
    if "margin" in v:
        out["margin"] = _num(v["margin"], f"{path}.margin", 0, 0.3)
    if "shape" in v:
        if v["shape"] not in ("circle", "rounded"):
            _err(f"{path}.shape", "circle or rounded")
        out["shape"] = v["shape"]
    if "show" in v:
        out["show"] = bool(v["show"])
    return out


SCENE_KEYS = ["at", "source", "shot", "fit", "background", "inset", "push", "punch", "transition", "bubble",
              "spotlight", "label"]


def spotlight(v, path, base):
    """false/none = off; true = the style's (or the focused window); {window, blur, dim}."""
    if v is None or v is False:
        return None
    if v is True:
        return dict(base) if base else {"window": {"focused": True}, "blur": 40.0, "dim": 0.35}
    _keys(v, path, ["window", "blur", "dim"])
    sel = v.get("window", (base or {}).get("window", {"focused": True}))
    if sel == "focused":
        sel = {"focused": True}
    _keys(sel, f"{path}.window", ["class", "title", "address", "focused"])
    if not sel:
        _err(f"{path}.window", "give class, title, address or focused: true")
    return {"window": dict(sel),
            "blur": _num(v.get("blur", (base or {}).get("blur", 40)), f"{path}.blur", 0, 200),
            "dim": _num(v.get("dim", (base or {}).get("dim", 0.35)), f"{path}.dim", 0, 0.95)}


def _scene(v, path, out_names, style, sources):
    _keys(v, path, SCENE_KEYS + list(out_names), ["at", "shot"])
    sc = {"at": _num(v["at"], f"{path}.at", 0), "source": v.get("source", next(iter(sources))),
          "label": v.get("label")}
    if sc["source"] not in sources:
        _err(f"{path}.source", f"unknown source {sc['source']!r} (have: {', '.join(sources)})")
    sc["shot"] = shot(v["shot"], f"{path}.shot")
    fit = v.get("fit", "auto")
    if fit not in FITS:
        _err(f"{path}.fit", f"expected one of {', '.join(FITS)}, got {fit!r}")
    sc["fit"] = fit
    sc["background"] = _background(v["background"], f"{path}.background") if "background" in v else None
    sc["push_raw"] = v.get("push", "style")
    if sc["push_raw"] != "style":
        push(sc["push_raw"], f"{path}.push")  # validate now
    sc["punch_raw"] = v.get("punch")
    punch(sc["punch_raw"], f"{path}.punch", style["punch"], sc["at"])  # validate now
    sc["inset_raw"] = v.get("inset")
    if sc["inset_raw"] is not None:
        inset(sc["inset_raw"], f"{path}.inset", DEFAULT_STYLE["inset"])  # validate now
    sc["transition"] = transition(v["transition"], f"{path}.transition") if "transition" in v \
        else transition(style["transition"], "style.transition")
    sc["bubble_raw"] = v.get("bubble")
    sc["spotlight_raw"] = v.get("spotlight", "style")
    if sc["spotlight_raw"] != "style":
        spotlight(sc["spotlight_raw"], f"{path}.spotlight", style["spotlight"])  # validate now
    sc["overrides"] = {}
    for name in out_names:
        if name in v:
            ov = v[name]
            _keys(ov, f"{path}.{name}", [k for k in SCENE_KEYS if k not in ("at", "source")])
            sc["overrides"][name] = ov
    return sc


def scene_for_output(sc, output, style, path, bubble_base=None):
    """A scene with one output's overrides applied (and re-validated). Bubble settings
    are sticky: they apply over `bubble_base` (the previous scene's), not the style."""
    ov = sc["overrides"].get(output)
    s = dict(sc)
    if ov:
        if "shot" in ov:
            s["shot"] = shot(ov["shot"], f"{path}.{output}.shot")
        if "fit" in ov:
            if ov["fit"] not in FITS:
                _err(f"{path}.{output}.fit", f"expected one of {', '.join(FITS)}")
            s["fit"] = ov["fit"]
        if "background" in ov:
            s["background"] = _background(ov["background"], f"{path}.{output}.background")
        if "transition" in ov:
            s["transition"] = transition(ov["transition"], f"{path}.{output}.transition")
        if "label" in ov:
            s["label"] = ov["label"]
    # Spotlight: per scene (not sticky) - the scene's value, else the style's.
    sp = ov["spotlight"] if ov and "spotlight" in ov else sc.get("spotlight_raw", "style")
    s["spotlight"] = style["spotlight"] if sp == "style" else spotlight(sp, f"{path}.spotlight", style["spotlight"])
    # Push-in: the scene's (or this output's override), else the style's. Not sticky.
    pr = ov["push"] if ov and "push" in ov else sc.get("push_raw", "style")
    s["push"] = style["push"] if pr == "style" else push(pr, f"{path}.push")
    # Punch-ins: the scene's (or this output's own list). Not sticky.
    pn = ov["punch"] if ov and "punch" in ov else sc.get("punch_raw")
    s["punch"] = punch(pn, f"{path}.punch", style["punch"], sc["at"])
    # Inset: the style's <- the scene's <- a pane's own.
    ins_raw = ov["inset"] if ov and "inset" in ov else sc.get("inset_raw")
    s["inset"] = inset(ins_raw, f"{path}.inset", style["inset"])
    if s["shot"]["kind"] == "split":
        panes = []
        for i, pane in enumerate(s["shot"]["panes"]):
            pane = dict(pane)
            pane["inset"] = inset(pane.get("inset_raw"), f"{path}.shot.split[{i}].inset", s["inset"])
            panes.append(pane)
        s["shot"] = dict(s["shot"], panes=panes)
    raw = ov["bubble"] if ov and "bubble" in ov else sc["bubble_raw"]
    s["bubble"] = bubble(raw, f"{path}.bubble", bubble_base or style["bubble"])
    return s


def scenes_for_output(scenes, output, style):
    """All scenes for one output, bubble settings carried forward per source."""
    out, last = [], {}
    for sc in scenes:
        s = scene_for_output(sc, output, style, f"scenes[{sc['index']}]", last.get(sc["source"]))
        last[sc["source"]] = s["bubble"]
        out.append(s)
    return out


# ------------------------------------------------------------------ script --
def parse(data, base_dir="."):
    """Validated, normalised script from the parsed YAML/JSON data."""
    if not isinstance(data, dict):
        raise ScriptError("edit.yaml: expected a mapping at the top level")
    _keys(data, "edit.yaml", ["version", "outputs", "sources", "timeline", "cuts", "audio", "style",
                              "intro", "outro", "scenes", "callouts", "overlays"], ["scenes"])
    if data.get("version", 1) != 1:
        _err("version", f"this pugrec understands version 1, not {data['version']!r}")

    outs = []
    for i, o in enumerate(data.get("outputs") or [{"name": "main", "size": "1920x1080"}]):
        p = f"outputs[{i}]"
        _keys(o, p, ["name", "size", "fps"], ["name", "size"])
        if not isinstance(o["name"], str) or not o["name"].isidentifier() or o["name"] in SCENE_KEYS:
            _err(f"{p}.name", f"a simple name like main or vertical, got {o['name']!r}")
        outs.append({"name": o["name"], "size": _size(o["size"], f"{p}.size"),
                     "fps": int(_num(o.get("fps", 60), f"{p}.fps", 1, 120))})
    if len({o["name"] for o in outs}) != len(outs):
        _err("outputs", "output names must be unique")
    out_names = [o["name"] for o in outs]

    srcs = data.get("sources") or {"main": {"session": "."}}
    sources = {}
    for name, s in srcs.items():
        p = f"sources.{name}"
        _keys(s, p, ["session", "video", "image"])
        if "session" not in s:
            _err(p, "only session sources are supported in this version (video/image: later)")
        sources[name] = {"kind": "session", "path": os.path.normpath(os.path.join(base_dir, s["session"]))}

    style = build_style(data.get("style"), base_dir)
    # A card from edit.yaml wins; else the theme's; `intro: false` / `outro: false` = none.
    tc = style.pop("theme_cards", {})
    intro = card(data["intro"] if "intro" in data else tc.get("intro"), "intro", style)
    outro = card(data["outro"] if "outro" in data else tc.get("outro"), "outro", style)

    audio = data.get("audio") or {}
    _keys(audio, "audio", ["source"])
    if audio.get("source", "mic-clean") not in ("mic-clean", "raw", "none"):
        _err("audio.source", "mic-clean, raw or none")

    cuts, auto_cuts = parse_cuts(data.get("cuts"))

    timeline = None
    if data.get("timeline"):
        timeline = []
        for i, seg in enumerate(data["timeline"]):
            p = f"timeline[{i}]"
            _keys(seg, p, ["source", "from", "to"], ["source"])
            if seg["source"] not in sources:
                _err(f"{p}.source", f"unknown source {seg['source']!r}")
            timeline.append({"source": seg["source"],   # from: None = the source's start marker
                             "from": None if seg.get("from") is None else _num(seg["from"], f"{p}.from", 0),
                             "to": None if seg.get("to") is None else _num(seg["to"], f"{p}.to", 0)})
        if cuts or auto_cuts:
            _err("cuts", "use either timeline (explicit segments) or cuts, not both")

    if data.get("overlays"):
        _err("overlays", "not supported yet (planned: picture-in-picture of other sources)")

    raw_scenes = data["scenes"]
    if not isinstance(raw_scenes, list) or not raw_scenes:
        _err("scenes", "expected a non-empty list")
    scenes = [_scene(s, f"scenes[{i}]", out_names, style, sources) for i, s in enumerate(raw_scenes)]
    for i, (a, b) in enumerate(zip(scenes, scenes[1:])):
        if a["source"] == b["source"] and b["at"] <= a["at"]:
            _err(f"scenes[{i + 1}].at", f"{b['at']} must be after the previous scene's {a['at']} (same source)")
    for i, s in enumerate(scenes):
        s["index"] = i
    check_sparkle_allowed(style, scenes)
    raw_callouts = data.get("callouts") or []
    if not isinstance(raw_callouts, list):
        _err("callouts", "expected a list of {at, title, text}")
    callouts = [callout(c, f"callouts[{i}]", style, sources) for i, c in enumerate(raw_callouts)]
    for i, c in enumerate(callouts):
        if c["sparkle"] and style["sparkle"] == DEFAULT_STYLE["sparkle"]:
            _err(f"callouts[{i}].sparkle", "sparkles only work with a theme that has a `sparkle:` section")
        if c["sparkle"] and c["look"] != "section":
            _err(f"callouts[{i}].sparkle", "only a section's wipe can sparkle")

    return {"version": 1, "outputs": outs, "sources": sources, "style": style,
            "audio": {"source": audio.get("source", "mic-clean")}, "cuts": cuts, "auto_cuts": auto_cuts,
            "timeline": timeline, "scenes": scenes, "intro": intro, "outro": outro, "callouts": callouts}


def _ranges(v, path):
    out = []
    for i, c in enumerate(v or []):
        p = f"{path}[{i}]"
        if not (isinstance(c, (list, tuple)) and len(c) == 2):
            _err(p, f"expected [from, to] in seconds, got {c!r}")
        a, b = _num(c[0], f"{p}[0]", 0), _num(c[1], f"{p}[1]", 0)
        if b <= a:
            _err(p, "to must be after from")
        out.append((a, b))
    return sorted(out)


def parse_cuts(v):
    """cuts: [[a, b], ...]  or  {ranges: [[a, b]], pauses: 1.2, fillers: true}.
    pauses/fillers come from the transcript at resolve time (pugrec transcript)."""
    if v is None:
        return [], None
    if isinstance(v, list):
        return _ranges(v, "cuts"), None
    _keys(v, "cuts", ["ranges", "pauses", "fillers"])
    auto = None
    if "pauses" in v or v.get("fillers"):
        auto = {"pauses": _num(v["pauses"], "cuts.pauses", 0.3, 30) if v.get("pauses") is not None else None,
                "fillers": bool(v.get("fillers", False))}
    return _ranges(v.get("ranges"), "cuts.ranges"), auto


def captions(v, path, base):
    if v is False or v is True:
        return dict(base, show=bool(v))
    _keys(v, path, list(base))
    out = dict(base)
    for k, val in v.items():
        if k in ("show", "highlight"):
            out[k] = bool(val)
        elif k == "size":
            out[k] = _num(val, f"{path}.size", 0.01, 0.2)
        elif k == "max_words":
            out[k] = int(_num(val, f"{path}.max_words", 1, 20))
        elif k == "position":
            if val not in ("auto", "bottom", "top", "middle"):
                _err(f"{path}.position", "auto, bottom, top or middle")
            out[k] = val
        elif k == "fix":
            if not isinstance(val, dict) or not all(isinstance(a, str) and isinstance(b, str) for a, b in val.items()):
                _err(f"{path}.fix", "a mapping of words to corrections, e.g. {Akme: Acme, Field Pal: FieldPal}")
            out[k] = {**base.get("fix", {}), **val}  # merges: a theme's brand names + this video's
        elif k == "font":
            if not isinstance(val, str) or not val.strip():
                _err(f"{path}.font", "a font family installed on the system, e.g. 'IBM Plex Sans'")
            out[k] = val.strip()
        elif k == "weight":
            out[k] = int(_num(val, f"{path}.weight", 100, 900))
        elif k in ("color", "background"):
            out[k] = _color(val, f"{path}.{k}")
        elif k == "background_opacity":
            out[k] = _num(val, f"{path}.{k}", 0, 1)
    return out


def _color(v, path):
    if not (isinstance(v, str) and len(v) == 7 and v.startswith("#")
            and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
        _err(path, f"a colour like '#16171a', got {v!r}")
    return v.lower()


def logo(v, path, base_dir):
    """{file, corner, size, margin, opacity}: a small watermark image (PNG/JPEG/WebP)."""
    if v is None or v is False:
        return None
    if isinstance(v, str):
        v = {"file": v}
    _keys(v, path, ["file", "corner", "size", "margin", "opacity"], ["file"])
    f = os.path.expanduser(str(v["file"]))
    f = f if os.path.isabs(f) else os.path.normpath(os.path.join(base_dir, f))
    if not os.path.isfile(f):
        _err(f"{path}.file", f"no such file: {f}")
    if not f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        _err(f"{path}.file", "PNG, JPEG or WebP (SVG: export it as PNG first)")
    return {"file": f, "corner": _corner(v.get("corner", "top-right"), f"{path}.corner"),
            "size": _num(v.get("size", 0.12), f"{path}.size", 0.02, 0.6),     # width, fraction of the output width
            "margin": _num(v.get("margin", 0.03), f"{path}.margin", 0, 0.3),
            "opacity": _num(v.get("opacity", 0.9), f"{path}.opacity", 0, 1)}


def card_style(v, path, base, base_dir="."):
    _keys(v, path, list(base))
    out = dict(base)
    for k, val in v.items():
        if k in ("background", "color"):
            out[k] = _color(val, f"{path}.{k}")
        elif k == "font":
            out[k] = None if val is None else str(val)
        elif k == "duration":
            out[k] = _num(val, f"{path}.duration", 0.5, 30)
        elif k == "image":  # a brand icon, big, centred above the title (like logo.file)
            out[k] = None if val in (None, False) else _image_file(val, f"{path}.image", base_dir)
        elif k == "image_size":
            out[k] = _num(val, f"{path}.image_size", 0.02, 0.6)
        elif k in ("fade_in", "fade_out"):
            out[k] = None if val is None and k == "fade_in" else _num(val, f"{path}.{k}", 0, 5)
        elif k == "title_case":
            if not isinstance(val, bool):
                _err(f"{path}.title_case", f"true or false, got {val!r}")
            out[k] = val
    return out


SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or",
               "the", "to", "via", "vs", "with"}


def title_case(s):
    """"decision and gated cards" -> "Decision and Gated Cards". Only all-lowercase words change:
    names and codes the author already cased (FieldPal, COUPA, KAL-132, iPhone) stay as written.
    Small words stay lowercase except first and last."""
    words = s.split(" ")
    last = max((i for i, w in enumerate(words) if w), default=0)
    out = []
    for i, w in enumerate(words):
        if not re.fullmatch(r"[a-z][a-z'-]*[:,.!?]?", w) or (w.rstrip(":,.!?") in SMALL_WORDS and 0 < i < last):
            out.append(w)
        else:
            out.append("-".join(p[:1].upper() + p[1:] for p in w.split("-")))
    return " ".join(out)


SPARKLY = ("sparkle", "sweep")


def check_sparkle_allowed(style, scenes):
    """sparkle/sweep transitions are a theme's signature, not a general effect (James: they
    must not make their way into other themes). Allowed only when the style configures
    `sparkle` - i.e. a theme (or the edit itself) chose it - never in a plain or other theme."""
    if style["sparkle"] != DEFAULT_STYLE["sparkle"]:
        return
    uses = []
    if transition(style["transition"], "style.transition")["type"] in SPARKLY:
        uses.append("style.transition")
    for s in scenes:
        if s["transition"]["type"] in SPARKLY:
            uses.append(f"scenes[{s['index']}].transition")
        for out, ov in s["overrides"].items():
            if "transition" in ov and transition(ov["transition"], "")["type"] in SPARKLY:
                uses.append(f"scenes[{s['index']}].{out}.transition")
    if uses:
        _err(uses[0], "sparkle/sweep transitions only work with a theme that has a `sparkle:` "
                      "section; use move, fade or cut here")


def sparkle_style(v, path, base, base_dir="."):
    _keys(v, path, list(base))
    out = dict(base)
    for k, val in v.items():
        p = f"{path}.{k}"
        if k == "colors":
            if val is None:
                out[k] = None
            elif not isinstance(val, list) or not val:
                _err(p, "a list of colours, e.g. ['#ffffff', '#ec1d83']")
            else:
                out[k] = [_color(c, f"{p}[{i}]") for i, c in enumerate(val)]
        elif k == "image":
            out[k] = None if val in (None, False) else _image_file(val, p, base_dir)
        elif k == "image_size":
            out[k] = _num(val, p, 0.05, 0.9)
        elif k in ("pixelated", "flip"):
            out[k] = bool(val)
        elif k == "count":
            out[k] = int(_num(val, p, 1, 200))
    return out


def backdrop_style(v, path, base, base_dir="."):
    """{gradient: [2-4 colours], angle} or {image: file}; one replaces the other."""
    _keys(v, path, list(base))
    out = dict(base)
    if "gradient" in v and "image" not in v:
        out["image"] = None  # a new gradient replaces an image set further down (a theme's)
    for k, val in v.items():
        p = f"{path}.{k}"
        if k == "gradient":
            if not isinstance(val, list) or not 2 <= len(val) <= 4:
                _err(p, "a list of 2-4 colours, e.g. ['#16171a', '#e8480f']")
            out[k] = [_color(c, f"{p}[{i}]") for i, c in enumerate(val)]
        elif k == "angle":
            out[k] = _num(val, p, -360, 360)
        elif k == "image":
            out[k] = None if val in (None, False) else _image_file(val, p, base_dir)
    return out


def _aspect(v, path):
    if v == "auto":
        return "auto"
    if isinstance(v, str) and ":" in v:
        try:
            a, b = (float(x) for x in v.split(":"))
        except ValueError:
            a = b = 0
        if a > 0 and b > 0:
            return round(a / b, 4)
        _err(path, f"expected auto, 'W:H' like '2:3', or a number, got {v!r}")
    return _num(v, path, 0.2, 5)


def inset(v, path, base):
    """Inset settings merged over `base` (the style's, or the scene's for a pane)."""
    if v is None:
        return dict(base)
    _keys(v, path, list(DEFAULT_STYLE["inset"]))
    out = dict(base)
    for k, val in v.items():
        p = f"{path}.{k}"
        if k == "padding":
            out[k] = _num(val, p, 0, 0.4)
        elif k == "radius":
            out[k] = _num(val, p, 0, 200)
        elif k == "aspect":
            out[k] = _aspect(val, p)
        elif k == "position":
            if not (isinstance(val, (list, tuple)) and len(val) == 2):
                _err(p, f"expected [x, y] (0..1 each), got {val!r}")
            out[k] = [_num(val[0], f"{p}[0]", 0, 1), _num(val[1], f"{p}[1]", 0, 1)]
        elif k == "shadow":
            out[k] = _num(val, p, 0, 1)
    return out


def push(v, path):
    """Scene/style push-in: false/null = off; true = the default (0.8 on wide outputs, 0.9 on tall
    ones, over 10 s); a number = `to`; {to, over}."""
    if v is None or v is False:
        return None
    if v is True:
        return {"to": None, "over": 10.0}
    if isinstance(v, (int, float)):
        return {"to": _num(v, path, 0.5, 0.99), "over": 10.0}
    _keys(v, path, ["to", "over"])
    return {"to": _num(v["to"], f"{path}.to", 0.5, 0.99) if v.get("to") is not None else None,
            "over": _num(v.get("over", 10.0), f"{path}.over", 1, 120)}


def punch(v, path, base, scene_at):
    """Scene punch-ins: a source time, or a list of them / of {at, zoom, dip, hold} (the rest from
    style.punch). `at` = when the zoom lands - put it on the word. Style: {zoom, dip, hold}."""
    if v is None or v is False:
        return []
    out = []
    for i, p in enumerate(v if isinstance(v, list) else [v]):
        pi = f"{path}[{i}]" if isinstance(v, list) else path
        if not isinstance(p, dict):
            p = {"at": p}
        _keys(p, pi, ["at", "zoom", "dip", "hold"], ["at"])
        at = _num(p["at"], f"{pi}.at", 0)
        if at < scene_at:
            _err(f"{pi}.at", f"{at} is before the scene starts ({scene_at})")
        out.append(dict(punch_style(p, pi, base), at=at))
    return sorted(out, key=lambda p: p["at"])


def punch_style(v, path, base):
    _keys(v, path, ["zoom", "dip", "hold"] + (["at"] if "at" in v else []))
    return {"zoom": _num(v.get("zoom", base["zoom"]), f"{path}.zoom", 0.01, 0.5),
            "dip": _num(v.get("dip", base["dip"]), f"{path}.dip", 0, 0.1),
            "hold": _num(v.get("hold", base["hold"]), f"{path}.hold", 0, 60)}


def _background(v, path):
    if v in ("blur", "backdrop") or (isinstance(v, str) and v.startswith("#") and len(v) == 7):
        return v
    _err(path, f"{BACKGROUNDS}, got {v!r}")


PANE_KEYS = ("fit", "inset", "background")  # allowed on a split pane, next to its shot


def _image_file(v, path, base_dir):
    f = os.path.expanduser(str(v))
    f = f if os.path.isabs(f) else os.path.normpath(os.path.join(base_dir, f))
    if not os.path.isfile(f):
        _err(path, f"no such file: {f}")
    if not f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        _err(path, "PNG, JPEG or WebP (SVG: export it as PNG first)")
    return f


def _choice(v, path, options):
    if v not in options:
        _err(path, f"expected one of {', '.join(options)}, got {v!r}")
    return v


def callout_style(v, path, base):
    _keys(v, path, list(base))
    out = dict(base)
    for k, val in v.items():
        p = f"{path}.{k}"
        if k == "look":
            out[k] = _choice(val, p, CALLOUT_LOOKS)
        elif k == "corner":
            out[k] = _choice(val, p, CALLOUT_CORNERS)
        elif k == "from":
            out[k] = _choice(val, p, ("auto",) + DIRECTIONS)
        elif k == "to":
            out[k] = _choice(val, p, DIRECTIONS)
        elif k in ("color", "background"):
            out[k] = _color(val, p)
        elif k == "border":
            out[k] = None if val is None else _color(val, p)
        elif k == "font":
            out[k] = None if val is None else str(val)
        elif k == "sparkle":
            if not isinstance(val, bool):
                _err(p, f"true or false, got {val!r}")
            out[k] = val
        elif k in ("weight", "text_weight"):
            out[k] = int(_num(val, p, 100, 900))
        elif k in ("size", "text_size"):
            out[k] = _num(val, p, 0.005, 0.2)
        elif k in ("margin", "padding", "distance"):
            out[k] = _num(val, p, 0, 0.5)
        elif k == "opacity":
            out[k] = _num(val, p, 0, 1)
        elif k == "duration":
            out[k] = _num(val, p, 0.5, 60)
        elif k == "pause":
            out[k] = _num(val, p, 0, 10)
        elif k in ("in", "out"):
            out[k] = _num(val, p, 0, 5)
        else:  # border_width, radius: px at 1080p
            out[k] = _num(val, p, 0, 200)
    return out


def callout(v, path, style, sources):
    """One callout: {at, title, text, duration, source} plus any style.callout key for this one."""
    if not isinstance(v, dict):
        _err(path, "expected {at, title, text}")
    own = {k: val for k, val in v.items() if k not in ("at", "title", "text", "source")}
    _keys(v, path, ["at", "title", "text", "source"] + list(style["callout"]), ["at", "title"])
    c = callout_style(own, path, style["callout"])
    c["at"] = _num(v["at"], f"{path}.at", 0)
    c["source"] = v.get("source", next(iter(sources)))
    if c["source"] not in sources:
        _err(f"{path}.source", f"unknown source {c['source']!r} (have: {', '.join(sources)})")
    c["title"] = str(v["title"])
    if c["look"] == "section" and style["card"]["title_case"]:   # a section is a card: the same house style
        c["title"] = title_case(c["title"])
    lines = [] if v.get("text") in (None, "") else str(v["text"]).split("\n")
    if len(lines) > 3:
        _err(f"{path}.text", "at most 3 lines: a callout is read in a glance")
    if lines and c["look"] == "pill":
        _err(f"{path}.text", "a pill is one short label: use look: draw or bar for a second line")
    c["lines"] = lines
    if c["in"] + c["out"] > c["duration"]:
        _err(f"{path}.duration", f"{c['duration']} is shorter than in + out ({c['in'] + c['out']})")
    return c


def card(v, path, style):
    """intro/outro: a title card. "Title" or {title, subtitle, duration, fade_in, fade_out}."""
    if v is None or v is False:
        return None
    if isinstance(v, str):
        v = {"title": v}
    _keys(v, path, ["title", "subtitle", "duration", "fade_in", "fade_out"], ["title"])
    cs = style["card"]
    fade_in = v.get("fade_in", cs["fade_in"])
    if fade_in is None:
        fade_in = 0.0 if path == "intro" else 0.4
    title = title_case(str(v["title"])) if cs["title_case"] else str(v["title"])
    return {"title": title, "subtitle": str(v["subtitle"]) if v.get("subtitle") else None,
            "duration": _num(v.get("duration", cs["duration"]), f"{path}.duration", 0.5, 30),
            "fade_in": _num(fade_in, f"{path}.fade_in", 0, 5),
            "fade_out": _num(v.get("fade_out", cs["fade_out"]), f"{path}.fade_out", 0, 5)}


# ------------------------------------------------------------------- style --
def themes_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "pugrec", "themes")


def list_themes():
    """{name: (path, description)} of the installed themes."""
    import yaml
    d = themes_dir()
    out = {}
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".yaml"))
    except FileNotFoundError:
        return out
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8") as f:
                desc = (yaml.safe_load(f) or {}).get("description", "")
        except (OSError, yaml.YAMLError, AttributeError):
            desc = "(unreadable)"
        out[n[:-5]] = (p, desc)
    return out


def theme_path(name, base_dir):
    if not isinstance(name, str) or not name:
        _err("style.theme", "a theme name or a path to a .yaml")
    if name.endswith(".yaml") or "/" in name:
        p = os.path.expanduser(name)
        p = p if os.path.isabs(p) else os.path.normpath(os.path.join(base_dir, p))
        if not os.path.isfile(p):
            _err("style.theme", f"no such theme file: {p}")
        return p
    p = os.path.join(themes_dir(), name + ".yaml")
    if not os.path.isfile(p):
        have = ", ".join(list_themes()) or f"none - put themes in {THEMES_HELP}"
        _err("style.theme", f"unknown theme {name!r} (available: {have})")
    return p


def load_theme(name, base_dir):
    """A theme file: style keys (plus an optional `description`), validated like edit.yaml's style."""
    import yaml
    p = theme_path(name, base_dir)
    with open(p, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ScriptError(f"theme {name}: not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ScriptError(f"theme {name}: expected a mapping of style keys")
    data = dict(data)
    data.pop("description", None)
    vocab = data.pop("vocabulary", None)  # for transcription (pugrec transcript --theme), not a style key
    if vocab is not None and not (isinstance(vocab, list) and all(isinstance(v, str) for v in vocab)):
        _err(f"theme {name}.vocabulary", "a list of words/names, e.g. [FieldPal, Acme]")
    if "theme" in data:
        _err(f"theme {name}", "a theme can't load another theme")
    return data, os.path.dirname(p)


def theme_vocabulary(name, base_dir="."):
    """A theme's `vocabulary:` list (names the transcript should get right), or []."""
    import yaml
    with open(theme_path(name, base_dir), encoding="utf-8") as f:
        v = (yaml.safe_load(f) or {}).get("vocabulary") or []
    if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
        _err(f"theme {name}.vocabulary", "a list of words/names, e.g. [FieldPal, Acme]")
    return v


def apply_style(style, st, path, base_dir):
    """Merge style keys `st` (from a theme or edit.yaml) over `style`, validating them."""
    _keys(st, path, list(DEFAULT_STYLE))
    for k, v in st.items():
        p = f"{path}.{k}"
        if k == "theme":
            continue  # handled by the caller
        if k == "bubble":
            style["bubble"] = bubble(v, p, style["bubble"])
        elif k == "click_rings":
            style[k] = bool(v)
        elif k in ("background", "accent"):
            style[k] = _color(v, p)
        elif k == "transition":
            transition(v, p)
            style[k] = v
        elif k == "spotlight":
            style[k] = spotlight(v, p, None)
        elif k == "captions":
            style[k] = captions(v, p, style[k])
        elif k == "logo":
            style[k] = logo(v, p, base_dir)
        elif k == "card":
            style[k] = card_style(v, p, style[k], base_dir)
        elif k == "callout":
            style[k] = callout_style(v, p, style[k])
        elif k == "sparkle":
            style[k] = sparkle_style(v, p, style[k], base_dir)
        elif k == "backdrop":
            style[k] = backdrop_style(v, p, style[k], base_dir)
        elif k == "inset":
            style[k] = inset(v, p, style[k])
        elif k == "push":
            style[k] = push(v, p)
        elif k == "punch":
            style[k] = punch_style(v, p, style[k])
        else:
            style[k] = _num(v, p, 0)
    return style


def build_style(st, base_dir):
    """Defaults <- the theme (if any) <- edit.yaml's style."""
    style = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_STYLE.items()}
    style["bubble"]["border"] = dict(style["bubble"]["border"])
    st = st or {}
    if not isinstance(st, dict):
        _err("style", "expected a mapping")
    style["theme_cards"] = {}  # intro/outro a theme brings (an edit's own intro/outro win)
    if st.get("theme"):
        data, tdir = load_theme(st["theme"], base_dir)
        style["theme_cards"] = {k: data.pop(k) for k in ("intro", "outro") if k in data}
        apply_style(style, data, f"theme {st['theme']}", tdir)
        style["theme"] = st["theme"]
    return apply_style(style, st, "style", base_dir)


def load(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ScriptError(f"{os.path.basename(path)}: not valid YAML: {e}") from None
    return parse(data, os.path.dirname(os.path.abspath(path)))
