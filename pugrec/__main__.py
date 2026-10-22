"""pugrec - record the screen plus what happened on it; render with automatic zoom.

  pugrec record [--keys] [--no-mic] [--mic SOURCE] [--no-input] [--no-camera] [--camera DEV] [--camera-size WxH]
                [--output DP-3] [--fps 60] [--dir DIR]
  pugrec stop
  pugrec toggle                      start, or stop if already recording (for a keybind)
  pugrec setup [--no-model]          make a checkout ready (.venv, whisper model; macOS: tools, helper); idempotent
  pugrec info SESSION
  pugrec faces SESSION               webcam face track -> faces.jsonl (derived)
  pugrec audio SESSION               cleaned mic -> mic-clean.flac (derived)
  pugrec transcript SESSION [--cuts] speech to text on the GPU -> transcript.jsonl (derived)
  pugrec check SESSION|edit.yaml     validate an edit script, list scenes and warnings per output
  pugrec build SESSION|edit.yaml [--output NAME] [--from S --to S]   edit -> build-<output>.mp4
                 [--whatsapp]        also a WhatsApp copy (H.264 Main, no B-frames, < 16 MB)
  pugrec whatsapp FILE.mp4 [-o OUT]  a copy WhatsApp plays inline -> FILE-whatsapp.mp4
  pugrec preview SESSION|edit.yaml   live browser preview, updates when edit.yaml is saved
  pugrec draft SESSION               a starting edit.yaml from the recording (focus, clicks, transcript)
  pugrec render SESSION [-o out.mp4] [--size 1920x1080] [--zoom 2] [--start S] [--end S]
"""

import argparse
import os
import subprocess
import sys

from . import jsonl, recorder


def cmd_record(a):
    if not recorder.claim_pidfile():
        print("pugrec: already recording (pugrec stop)", file=sys.stderr)
        return 1
    if sys.platform == "darwin":
        from .recorder_mac import MacSession as Session
    else:
        Session = recorder.Session
    try:
        Session(a).run()
    finally:
        recorder.release_pidfile()
    return 0


def cmd_toggle(a):
    if recorder.running_pid():
        return recorder.stop()
    # Detach, so a keybind's exec returns at once. Append (never truncate: a second recorder
    # once overwrote the first's log, 2026-10-13_19-20-50), one header per recording, kept small.
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pugrec.log")
    try:
        if os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass
    log = open(path, "a", buffering=1)
    p = subprocess.Popen([sys.executable, "-m", "pugrec", "record", *a.rest], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import datetime
    log.write(f"=== {datetime.datetime.now().isoformat(timespec='seconds')}  pugrec record (pid {p.pid}) ===\n")
    return 0


def cmd_info(a):
    recs = jsonl.read(os.path.join(a.session, "session.jsonl"))
    counts = {}
    for r in recs:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    start = next((r for r in recs if r["type"] == "session_start"), {})
    end = next((r for r in recs if r["type"] == "session_end"), None)
    print(f"started   {start.get('started')}  on {start.get('host')}  sync: {start.get('sync')}")
    print(f"duration  {end['duration'] / 1e9:.1f} s" if end else "duration  ? (no session_end - crashed or still recording)")
    from .edit.resolve import last_pts
    problems = []
    total = end["duration"] / 1e9 if end else None
    for r in recs:
        if r["type"] == "stream_start":
            print(f"stream    {r['stream']}: {r['file']} {r.get('pixels')} {r.get('codec')} audio={r.get('audio')}")
            # Where the tracks really end: a stalled encoder leaves sound with no picture, and
            # the container duration hides it (2026-10-13_19-20-50: video 63.8 s, audio 94.2 s).
            path = os.path.join(a.session, r["file"])
            off = r.get("first_frame_t", 0) / 1e9
            v = last_pts(path, "v:0")
            au = last_pts(path, "a:0") if r.get("audio") else None
            if v is None:
                print("          video: ? (no readable video packets)")
                problems.append(f"{r['stream']}: no readable video")
                continue
            v += off
            print(f"          video ends {v:7.2f}s" + (f", audio ends {au + off:7.2f}s" if au is not None else ""))
            if au is not None and au + off - v > 2.0:
                problems.append(f"{r['stream']}: video stops at {v:.1f}s but audio runs to {au + off:.1f}s "
                                f"({au + off - v:.1f}s of sound with no picture)")
            elif total and total - v > 3.0:
                problems.append(f"{r['stream']}: video stops at {v:.1f}s, the recording ran to {total:.1f}s")
        elif r["type"] == "notice":
            print(f"notice    {r['t'] / 1e9:7.2f}s  {r['message']}")
    print("events    " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for p in problems:
        print(f"WARNING   {p}")
    return 1 if problems else 0


def cmd_transcript(a):
    from . import transcript
    transcript.transcribe(a.session, a.language, prompt=a.prompt, theme=a.theme)
    if a.cuts:
        cuts = transcript.suggest_cuts(transcript.load(a.session))
        print("cuts:" + ("" if cuts else " []"))
        for c in cuts:
            print(f"  - [{c[0]}, {c[1]}]")
    return 0


def edit_path(p):
    return os.path.join(p, "edit.yaml") if os.path.isdir(p) else p


def cmd_check(a):
    """Validate an edit and show what it resolves to - the quick loop while scripting."""
    from .edit import resolve, script
    try:
        sc = script.load(edit_path(a.edit))
    except script.ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sessions = resolve.load_sessions(sc)
    for out in sc["outputs"]:
        p = resolve.resolve(sc, out["name"], sessions=sessions, fps=10)
        print(f"{out['name']} {out['size'][0]}x{out['size'][1]}: {p['duration']:.1f}s, "
              f"{len(p['segments'])} segment(s)")
        for s in p["scenes"]:
            when = "not shown (cut)" if s["out"] is None else f"{s['out']:6.2f}s"
            print(f"  [{s['index']}] source {s['at']:6.2f}s -> {when}  {s['label']}")
        for w in p["warnings"]:
            print(f"  warning: {w}")
    return 0


def ensure_derived(sc):
    """Face tracks the plan needs (camera framing, bubble), computed once if missing."""
    from . import faces
    for s in sc["sources"].values():
        if faces.needed(s["path"]):
            faces.detect(s["path"])


def cmd_build(a):
    from .edit import compose, resolve, script
    path = edit_path(a.edit)
    try:
        sc = script.load(path)
    except script.ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    ensure_derived(sc)
    sessions = resolve.load_sessions(sc)
    names = [o["name"] for o in sc["outputs"]] if a.output == "all" else [a.output]
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(path))
    try:  # now, not after minutes of rendering (the final mux can't create it)
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print(f"error: --out-dir {out_dir}: {e.strerror}", file=sys.stderr)
        return 1
    rc = 0
    for name in names:
        if name not in [o["name"] for o in sc["outputs"]]:
            print(f"error: no output {name!r} in the script", file=sys.stderr)
            return 1
        p = resolve.resolve(sc, name, sessions=sessions, fps=a.fps, t_from=a.start, t_to=a.end)
        for w in p["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        suffix = "" if a.start == 0 and a.end is None else f"-{a.start:g}-{a.end or p['duration']:g}"
        crf, preset = (min(a.crf, 14), "slow" if a.preset == "medium" else a.preset) if a.hq else (a.crf, a.preset)
        out = os.path.join(out_dir, f"build-{name}{suffix}.mp4")
        r = compose.build(p, out, crf=crf, preset=preset, jobs=a.jobs)
        rc |= r
        if a.whatsapp and not r:
            rc |= cmd_whatsapp(argparse.Namespace(file=out, out=None))
    return rc


def cmd_whatsapp(a):
    from . import share
    try:
        share.whatsapp(a.file, a.out)
    except share.ShareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_draft(a):
    from .edit import draft
    path, n = draft.draft(a.session)
    print(f"draft: {n} scenes -> {path}  (next: pugrec check {path})", file=sys.stderr)
    return cmd_check(argparse.Namespace(edit=path))


def cmd_themes(a):
    from .edit import script
    import yaml
    if a.defaults:  # today's PugPal look as a theme file (a starting point for pugpal.yaml)
        # sparkle stays out: it's one theme's signature, not part of a starting point.
        st = {k: v for k, v in script.DEFAULT_STYLE.items() if k not in ("theme", "spotlight", "logo", "sparkle")}
        st["captions"] = {k: v for k, v in st["captions"].items() if k != "fix"}
        print("description: The PugPal look (pugrec's defaults)")
        print(yaml.safe_dump(st, sort_keys=False, default_flow_style=None).rstrip())
        return 0
    themes = script.list_themes()
    if not themes:
        print(f"no themes in {script.themes_dir()} (pugrec themes --defaults prints a starting point)")
        return 0
    for name, (path, desc) in themes.items():
        print(f"{name:<20} {desc}")
    return 0


def cmd_render(a):
    from . import render
    w, h = (int(v) for v in a.size.lower().split("x"))
    if w % 2 or h % 2:
        raise SystemExit("--size must be even (H.264 4:2:0)")
    out = a.out or os.path.join(a.session, f"render-{w}x{h}.mp4")
    return render.render(a.session, out, size=(w, h), fps=a.fps, zoom=a.zoom,
                         start=a.start, end=a.end, rings=not a.no_rings, crf=a.crf, camera=not a.no_camera)


def main(argv=None):
    p = argparse.ArgumentParser(prog="pugrec", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record until `pugrec stop` or Ctrl+C")
    r.add_argument("--output", help="monitor to record (default: the focused one; macOS: the one with the cursor)")
    r.add_argument("--fps", type=int, default=60)
    r.add_argument("--mic", help="PulseAudio/PipeWire source; macOS: mic name or ID (default: the default source)")
    r.add_argument("--no-mic", action="store_true")
    r.add_argument("--keys", action=argparse.BooleanOptionalAction, default=None,
                   help="also record key presses: asks for your password (root helper; macOS: needs Input "
                        "Monitoring for the terminal, once). Default: clicks only, no prompt; `keys = true` in ~/.config/pugrec/config.toml makes --keys the default")
    r.add_argument("--no-input", action="store_true", help="no keys and no clicks at all")
    r.add_argument("--camera", help="V4L2 device (default: first /dev/v4l/by-id/*-video-index0); "
                                    "macOS: camera name or ID (default: the system default)")
    r.add_argument("--camera-size", default="1920x1080", help="MJPEG mode, e.g. 3840x2160 (default 1920x1080)")
    r.add_argument("--no-camera", action="store_true")
    r.add_argument("--countdown", type=int, default=3,
                   help="seconds of 3-2-1 after the camera/keys are ready; the edit starts after it (0 = none)")
    r.add_argument("--dir", help="parent folder for sessions (default: ~/Videos/PugRec)")
    r.add_argument("--exclude-pid", type=int, help=argparse.SUPPRESS)  # macOS: an app kept out of the video
    r.set_defaults(fn=cmd_record)

    s = sub.add_parser("stop", help="stop the running recording")
    s.set_defaults(fn=lambda a: recorder.stop())

    t = sub.add_parser("toggle", help="start (with these record options) or stop")
    t.add_argument("rest", nargs=argparse.REMAINDER)
    t.set_defaults(fn=cmd_toggle)

    i = sub.add_parser("info", help="summarise a session")
    i.add_argument("session")
    i.set_defaults(fn=cmd_info)

    fc = sub.add_parser("faces", help="track the face in the camera file -> faces.jsonl (render does this if missing)")
    fc.add_argument("session")
    fc.set_defaults(fn=lambda a: __import__("pugrec.faces").faces.detect(a.session) and 0)

    au = sub.add_parser("audio", help="clean the mic (noise, keyboard, breaths, tone, loudness) -> mic-clean.flac")
    au.add_argument("session")
    au.set_defaults(fn=lambda a: __import__("pugrec.audio").audio.clean(a.session) and 0)

    tr = sub.add_parser("transcript", help="speech to text on the GPU -> transcript.jsonl")
    tr.add_argument("session")
    tr.add_argument("--language", default="auto")
    tr.add_argument("--prompt", help="names/words to spell right, comma-separated (e.g. 'FieldPal, Acme')")
    tr.add_argument("--theme", help="also use this theme's `vocabulary:` list")
    tr.add_argument("--cuts", action="store_true", help="also print suggested cuts (pauses, fillers) as YAML")
    tr.set_defaults(fn=cmd_transcript)

    ck = sub.add_parser("check", help="validate an edit.yaml and show its scenes/warnings per output")
    ck.add_argument("edit", help="edit.yaml, or a session folder containing one")
    ck.set_defaults(fn=cmd_check)

    b = sub.add_parser("build", help="render an edit.yaml to MP4 (one per output)")
    b.add_argument("edit", help="edit.yaml, or a session folder containing one")
    b.add_argument("--output", default="all", help="output name, or all (default)")
    b.add_argument("--from", dest="start", type=float, default=0.0, help="output seconds (quick checks)")
    b.add_argument("--to", dest="end", type=float)
    b.add_argument("--fps", type=int, help="override the output fps (e.g. 30 for a quick look)")
    b.add_argument("--crf", type=int, default=18, help="x264 quality: lower = better/bigger (14 ~ visually lossless)")
    b.add_argument("--preset", default="medium", help="x264 preset: slower = smaller at the same quality (slow, veryslow)")
    b.add_argument("--hq", action="store_true", help="high quality: --crf 14 --preset slow")
    b.add_argument("--jobs", type=int, default=4, help="parallel render processes (default 4)")
    b.add_argument("--out-dir", help="default: next to edit.yaml")
    b.add_argument("--whatsapp", action="store_true", help="also write build-<output>-whatsapp.mp4 (plays inline)")
    b.set_defaults(fn=cmd_build)

    wa = sub.add_parser("whatsapp", help="make a copy of an MP4 that WhatsApp plays inline (< 16 MB)")
    wa.add_argument("file")
    wa.add_argument("-o", "--out", help="default: FILE-whatsapp.mp4")
    wa.set_defaults(fn=cmd_whatsapp)

    pv = sub.add_parser("preview", help="live preview of an edit in the browser (updates on save)")
    pv.add_argument("edit", help="edit.yaml, or a session folder containing one")
    pv.add_argument("--port", type=int, default=8765)
    pv.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    pv.set_defaults(fn=lambda a: __import__("pugrec.edit.preview").edit.preview.serve(
        edit_path(a.edit), a.port, not a.no_open) or 0)

    px = sub.add_parser("proxy", help="make the browser proxies for a session (preview does this on demand)")
    px.add_argument("session")
    px.set_defaults(fn=lambda a: [__import__("pugrec.edit.preview").edit.preview.make_proxies(
        __import__("pugrec.edit.resolve").edit.resolve.Session(a.session))] and 0)

    dr = sub.add_parser("draft", help="write a starting edit.yaml (edit.draft.yaml if one exists)")
    dr.add_argument("session")
    dr.set_defaults(fn=cmd_draft)

    th = sub.add_parser("themes", help="list the themes in ~/.config/pugrec/themes/")
    th.add_argument("--defaults", action="store_true", help="print the default (PugPal) style as a theme file")
    th.set_defaults(fn=cmd_themes)

    su = sub.add_parser("setup", help="make this checkout ready: .venv (when requirements changed) + whisper model; "
                                      "macOS: also Homebrew tools and the capture helper")
    su.add_argument("--no-model", action="store_true", help="skip the whisper model download (874 MB)")
    su.set_defaults(fn=lambda a: __import__("pugrec.setup").setup.run(a.no_model))

    d = sub.add_parser("render", help="render with automatic zoom to a normal-sized MP4")
    d.add_argument("session")
    d.add_argument("-o", "--out")
    d.add_argument("--size", default="1920x1080")
    d.add_argument("--fps", type=int)
    d.add_argument("--zoom", type=float, default=2.0, help="max zoom relative to the widest view")
    d.add_argument("--start", type=float, default=0.0)
    d.add_argument("--end", type=float)
    d.add_argument("--crf", type=int, default=18)
    d.add_argument("--no-rings", action="store_true", help="no click highlight rings")
    d.add_argument("--no-camera", action="store_true", help="no webcam bubble")
    d.set_defaults(fn=cmd_render)

    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
