"""Regression snapshots: what every existing edit resolves to, so additions can prove they change nothing.

A snapshot of one edit is its `check` output, and per output the resolved plan (everything but the
frames, plus one hash per frame) and the build's audio graph. `pugrec regress save` records them for
every edit under the given folders (default ~/Videos/PugRec, ~/Videos/PugTake) into
~/.cache/pugrec/regress/ - outside the repo, because plans carry local paths and on-screen text.
`pugrec regress compare` re-resolves and reports the first difference per edit. The repo's own
gate (tests/test_regress.py) snapshots fixture edits on a fake session the same way.
"""

import argparse
import contextlib
import glob
import hashlib
import io
import json
import os
import sys

DEFAULT_DIRS = ["~/Videos/PugRec", "~/Videos/PugTake"]
STORE = os.path.expanduser("~/.cache/pugrec/regress")


def _dump(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _hash(v):
    return hashlib.sha256(_dump(v).encode()).hexdigest()[:16]


def plan_snapshot(p, audio=True):
    """A plan without its frames (they're hashed one by one) + the audio graph the build would run."""
    from . import compose
    meta = {k: v for k, v in p.items() if k != "frames"}
    snap = {"plan": meta, "frames": [_hash(f) for f in p["frames"]]}
    if audio:
        inputs, graph = compose.audio_args(p, p["frames"], 1)
        snap["audio"] = {"inputs": inputs, "graph": graph}
    return snap


def check_output(path):
    """Exactly what `pugrec check` prints (stdout + stderr) and its exit code."""
    from .. import __main__ as cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = cli.cmd_check(argparse.Namespace(edit=path))
        except Exception as e:  # an edit that crashes check is a state worth recording too
            rc = f"exception: {type(e).__name__}: {e}"
    return {"rc": rc, "text": out.getvalue()}


def snapshot(path):
    """Everything an old edit must keep producing: check output, and per output plan + audio graph."""
    from . import resolve, script
    snap = {"check": check_output(path), "outputs": {}}
    try:
        sc = script.load(path)
        sessions = resolve.load_sessions(sc)
        for out in sc["outputs"]:
            snap["outputs"][out["name"]] = plan_snapshot(resolve.resolve(sc, out["name"], sessions=sessions))
    except Exception as e:
        snap["error"] = f"{type(e).__name__}: {e}"
    return snap


def diff(old, new, where=""):
    """The first few differences between two snapshots (paths into the JSON), [] if identical."""
    out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new), key=str):
            if k not in old or k not in new:
                out.append(f"{where}/{k}: {'added' if k not in old else 'removed'}")
            else:
                out += diff(old[k], new[k], f"{where}/{k}")
            if len(out) >= 5:
                break
    elif isinstance(old, list) and isinstance(new, list) and where.endswith("/frames"):
        if len(old) != len(new):
            out.append(f"{where}: {len(old)} -> {len(new)} frames")
        bad = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
        if bad:
            out.append(f"{where}: {len(bad)} frame(s) differ, first #{bad[0]}")
    elif old != new:
        a, b = _dump(old), _dump(new)
        out.append(f"{where}: {a[:120]} -> {b[:120]}")
    return out


def edits(dirs):
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(os.path.expanduser(d), "**", "*.yaml"), recursive=True)):
            if f"{os.sep}out{os.sep}" not in p:
                yield p


def _key(path):
    return hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16] + ".json"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pugrec regress", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["save", "compare"])
    ap.add_argument("dirs", nargs="*", default=DEFAULT_DIRS)
    ap.add_argument("--store", default=STORE)
    a = ap.parse_args(argv)
    os.makedirs(a.store, exist_ok=True)
    bad = 0
    for path in edits(a.dirs):
        snap = snapshot(path)
        file = os.path.join(a.store, _key(path))
        if a.action == "save":
            with open(file, "w") as f:
                json.dump({"path": path, "snapshot": snap}, f)
            print(f"saved    {path}")
            continue
        if not os.path.exists(file):
            print(f"new      {path} (no snapshot)")
            continue
        with open(file) as f:
            old = json.load(f)["snapshot"]
        d = diff(old, snap)
        bad += bool(d)
        print(f"{'CHANGED' if d else 'same':8} {path}")
        for line in d:
            print(f"    {line}")
    if a.action == "compare":
        print(f"{bad} edit(s) changed" if bad else "every edit unchanged")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
