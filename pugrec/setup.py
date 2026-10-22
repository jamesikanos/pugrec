"""`pugrec setup`: make a checkout ready to use. Idempotent, stdlib only (runs on the
system Python, before any venv exists) - the PugPal dotfiles' install.sh calls it.

  1. .venv from requirements.txt - built or updated only when requirements.txt or the
     Python version changed (hash marker inside .venv)
  2. the whisper model in ~/.cache/pugrec, pinned SHA-256 (skip with --no-model):
     downloaded to .part, verified, renamed; skipped if present and valid
  3. macOS: the capture helper (macos/, Swift) built when missing or older than its sources
  4. macOS, first: the Xcode Command Line Tools (swift) and, from Homebrew, whichever of ffmpeg,
     whisper-cpp and the IBM Plex fonts are missing
Exit 0 when done; non-zero with one line "setup: <reason>" otherwise.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request

from . import transcript

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(REPO, ".venv")
REQS = os.path.join(REPO, "requirements.txt")
MARKER = os.path.join(VENV, ".pugrec-requirements.sha256")
# macOS: command on PATH -> the Homebrew formula that provides it; the fonts are casks.
BREW_FORMULAE = {"ffmpeg": "ffmpeg", "whisper-cli": "whisper-cpp"}
BREW_CASKS = ["font-ibm-plex-sans", "font-ibm-plex-mono"]  # captions + preview


class SetupError(Exception):
    pass


def _say(msg):
    print(f"setup: {msg}", file=sys.stderr)


def wanted_marker():
    with open(REQS, "rb") as f:
        reqs = hashlib.sha256(f.read()).hexdigest()
    return f"{reqs} python{sys.version_info.major}.{sys.version_info.minor}"


def venv():
    want = wanted_marker()
    py = os.path.join(VENV, "bin", "python")
    try:
        have = open(MARKER).read().strip()
    except OSError:
        have = None
    if have == want and os.access(py, os.X_OK):
        _say(".venv up to date")
        return
    # No marker = a fresh or failed install, maybe with another Python: start clean then too.
    rebuild = not os.access(py, os.X_OK) or not have or have.split()[-1] != want.split()[-1]
    if rebuild:  # missing, or made by another Python version: start clean
        _say("creating .venv")
        r = subprocess.run([sys.executable, "-m", "venv", "--clear", VENV], capture_output=True, text=True)
        if r.returncode:
            raise SetupError(f"python -m venv failed: {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
    _say("installing requirements.txt into .venv")
    r = subprocess.run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", REQS],
                       capture_output=True, text=True)
    if r.returncode:
        last = (r.stderr or r.stdout).strip().splitlines()
        raise SetupError(f"pip install failed: {last[-1] if last else 'exit ' + str(r.returncode)}")
    with open(MARKER, "w") as f:
        f.write(want + "\n")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def model():
    d = transcript._cache()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, transcript.MODEL)
    if os.path.exists(path):
        if _sha256(path) == transcript.MODEL_SHA256:
            _say(f"whisper model present ({transcript.MODEL})")
            return
        _say(f"{path} doesn't match the pinned SHA-256: downloading again")
    part = path + ".part"
    _say(f"downloading {transcript.MODEL_URL}")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(transcript.MODEL_URL, timeout=60) as r, open(part, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
    except OSError as e:
        raise SetupError(f"model download failed: {e}") from None
    if h.hexdigest() != transcript.MODEL_SHA256:
        os.unlink(part)
        raise SetupError("downloaded model doesn't match the pinned SHA-256 (deleted)")
    os.replace(part, path)
    _say(f"whisper model ready ({path})")


def capture_helper():
    """macOS: swift build -c release in macos/ when the binary is missing or out of date."""
    src = os.path.join(REPO, "macos")
    binary = os.path.join(src, ".build", "release", "pugrec-capture")
    newest = max(os.path.getmtime(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(src, "Sources"))
                 for f in fs)
    newest = max(newest, os.path.getmtime(os.path.join(src, "Package.swift")))
    if os.path.exists(binary) and os.path.getmtime(binary) >= newest:
        _say("pugrec-capture up to date")
        return
    _say("building pugrec-capture (swift build -c release)")
    r = subprocess.run(["swift", "build", "-c", "release", "--package-path", src], capture_output=True, text=True)
    if r.returncode:
        errs = [l for l in (r.stderr + r.stdout).splitlines() if "error" in l]
        raise SetupError(f"swift build failed: {errs[0] if errs else 'exit ' + str(r.returncode)}")


def mac_tools():
    """macOS: the Xcode Command Line Tools, then what's missing from Homebrew (its output shown:
    a first install takes minutes)."""
    if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
        subprocess.run(["xcode-select", "--install"], capture_output=True)  # opens Apple's installer
        raise SetupError("needs the Xcode Command Line Tools: finish the installer that just opened, "
                         "then run setup again")
    brew = shutil.which("brew") or next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if os.access(b, os.X_OK)), None)
    if not brew:
        raise SetupError("needs Homebrew (https://brew.sh), then run setup again")
    formulae = [f for cmd, f in BREW_FORMULAE.items() if not shutil.which(cmd)]
    have = subprocess.run([brew, "list", "--cask"], capture_output=True, text=True).stdout.split()
    casks = [c for c in BREW_CASKS if c not in have]
    for kind, names in (("--formula", formulae), ("--cask", casks)):
        if not names:
            continue
        _say(f"brew install {' '.join(names)}")
        if subprocess.run([brew, "install", kind, *names]).returncode:
            raise SetupError(f"brew install {' '.join(names)} failed (see above)")
    if not formulae and not casks:
        _say("Homebrew tools present")


def run(no_model=False):
    try:
        if sys.platform == "darwin":
            mac_tools()
            capture_helper()
        venv()
        if not no_model:
            model()
    except SetupError as e:
        print(f"setup: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"setup: {e}", file=sys.stderr)
        return 1
    _say("done")
    return 0
