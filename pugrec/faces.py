"""Face track for the webcam: `pugrec faces SESSION` -> SESSION/faces.jsonl.

Derived data (recomputable from camera.mkv, macOS camera.mov), so it's a separate file,
never part of session.jsonl. One line per sampled camera frame with a face:
    {"t": <session ns>, "x": .., "y": .., "w": .., "h": .., "score": ..}
x/y/w/h = the face box as fractions of the camera frame (0..1), top-left origin.
Frames without a face are simply absent. The editor and the test render use it
to keep the face centred in a camera crop.

Detector: OpenCV's YuNet (bundled API, small ONNX model downloaded once to
~/.cache/pugrec, pinned by SHA-256).
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request

from . import jsonl
from .render import _read_exact

MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SAMPLE_FPS = 10
SAMPLE_W, SAMPLE_H = 960, 540
MIN_SCORE = 0.8


def model_path():
    d = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pugrec")
    p = os.path.join(d, os.path.basename(MODEL_URL))
    if not os.path.exists(p):
        os.makedirs(d, exist_ok=True)
        print(f"faces: downloading {MODEL_URL}", file=sys.stderr)
        tmp = p + ".part"
        urllib.request.urlretrieve(MODEL_URL, tmp)
        os.replace(tmp, p)
    with open(p, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != MODEL_SHA256:
            raise SystemExit(f"faces: {p} doesn't match the pinned SHA-256; delete it to re-download")
    return p


def detect(session_dir):
    import cv2
    import numpy as np

    recs = jsonl.read(os.path.join(session_dir, "session.jsonl"))
    cam = next((r for r in recs if r["type"] == "stream_start" and r["stream"] == "camera"), None)
    if not cam:
        raise SystemExit("faces: this session has no camera stream")
    offset = cam["first_frame_t"]
    det = cv2.FaceDetectorYN.create(model_path(), "", (SAMPLE_W, SAMPLE_H), MIN_SCORE)
    # fps= from the camera's PTS 0: frame i is at camera time i / SAMPLE_FPS.
    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", os.path.join(session_dir, cam["file"]),
         "-vf", f"fps={SAMPLE_FPS}:start_time=0,scale={SAMPLE_W}:{SAMPLE_H}",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE, bufsize=0)
    out_path = os.path.join(session_dir, "faces.jsonl")
    n = found = 0
    size = SAMPLE_W * SAMPLE_H * 3
    with open(out_path + ".part", "w") as out:
        while True:
            raw = _read_exact(dec.stdout, size)  # a plain read() can return part of a frame
            if raw is None:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3)
            _, faces = det.detect(frame)
            if faces is not None and len(faces):
                face = max(faces, key=lambda f: f[2] * f[3])  # the largest = nearest
                x, y, w, h = face[:4]
                score = float(face[-1])
                out.write(json.dumps({"t": offset + int(n * 1e9 / SAMPLE_FPS),
                                      "x": round(float(x) / SAMPLE_W, 4), "y": round(float(y) / SAMPLE_H, 4),
                                      "w": round(float(w) / SAMPLE_W, 4), "h": round(float(h) / SAMPLE_H, 4),
                                      "score": round(score, 3)}) + "\n")
                found += 1
            n += 1
    dec.wait()
    os.replace(out_path + ".part", out_path)  # derived: written whole, then swapped in
    print(f"faces: {found}/{n} sampled frames with a face -> {out_path}", file=sys.stderr)
    return out_path


def camera_file(session_dir):
    """The camera's file from its stream_start (camera.mkv, macOS camera.mov), or None."""
    p = os.path.join(session_dir, "session.jsonl")
    if not os.path.exists(p):
        return None
    cam = next((r for r in jsonl.read(p) if r["type"] == "stream_start" and r["stream"] == "camera"), None)
    return os.path.join(session_dir, cam["file"]) if cam else None


def needed(session_dir):
    """True when the session has a camera but no face track yet (build/preview make it then)."""
    if os.path.exists(os.path.join(session_dir, "faces.jsonl")):
        return False
    cam = camera_file(session_dir)
    return bool(cam) and os.path.exists(cam)


def load(session_dir):
    """[(t_seconds, cx, cy, h)] in camera fractions, or None if there's no faces.jsonl."""
    p = os.path.join(session_dir, "faces.jsonl")
    if not os.path.exists(p):
        return None
    return [(r["t"] / 1e9, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2, r["h"]) for r in jsonl.read(p)]
