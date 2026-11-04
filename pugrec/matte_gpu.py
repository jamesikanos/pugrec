"""RVM matting on the GPU (PyTorch ROCm): raw rgb24 frames on stdin -> gray alpha frames on stdout.

Run by cutout.py with a Python that has torch - by default pugcam's venv (/var/lib/pugcam/venv: PyTorch
for ROCm is ~16 GB, too big for pugrec's .venv on the home disk). Standard library + torch + numpy only,
run with -I: nothing of pugrec is imported. The same maths as cutout.Matter (onnxruntime, the CPU path
and the reference): fp32, src / 255, downsample_ratio min(1, 270 / h), alpha * 255 + 0.5 to uint8.

  python -I matte_gpu.py MODEL W H N SKIP [--device cuda|cpu] [--vram-mb MB]

N frames are matted in order (recurrent); the first SKIP are run but not written (RVM's warm-up). If
stdin ends early the last frame is held, as the CPU path does. The desktop shares the GPU, so:
  exit 99  GPU memory over the baseline at start + MB (default 2048)
  exit 98  pugcam went live (a call wants the camera and the GPU)
and the caller falls back to the CPU.
"""

import json
import os
import socket
import sys
import time

import numpy as np
import torch

VRAM = "/sys/class/drm/renderD128/device/mem_info_vram_used"
PUGCAM_SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "pugcam.sock")


def vram_mb():
    try:
        with open(VRAM) as f:
            return int(f.read()) // (1 << 20)
    except OSError:
        return None


def pugcam_live():
    """True while pugcam is in a call (its session isn't idle/off). Not running = not live."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(1)
        s.connect(PUGCAM_SOCK)
        s.sendall(b'{"get": true}\n')
        st = json.loads(s.makefile(encoding="utf-8").readline())
        s.close()
        return st.get("session") not in ("idle", "off", None)
    except (OSError, ValueError):
        return False


def read_frame(f, size):
    buf = bytearray()
    while len(buf) < size:
        b = f.read(size - len(buf))
        if not b:
            return None
        buf += b
    return buf


def main(argv):
    model, w, h, n, skip = argv[0], *map(int, argv[1:5])
    opts = dict(zip(argv[5::2], argv[6::2]))
    dev = torch.device(opts.get("--device", "cuda"))
    limit = int(opts.get("--vram-mb", 2048))
    gpu = dev.type == "cuda"
    if gpu and pugcam_live():   # checked here too, not just by the caller: never start on a call's GPU
        print("matte_gpu: pugcam is in a call: leaving it the GPU", file=sys.stderr)
        return 98
    if gpu and not torch.cuda.is_available():   # ROCm builds of PyTorch use the "cuda" name
        print("matte_gpu: PyTorch sees no GPU (ROCm) - check /dev/kfd", file=sys.stderr)
        return 2
    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
    base = vram_mb() if gpu else None
    if gpu:   # torch's own cap as well as the sysfs watch (which also sees everyone else's use)
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(0.9, limit * (1 << 20) / total))
    net = torch.jit.freeze(torch.jit.load(model, map_location=dev).eval())
    ratio = min(1.0, 270 / h)
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    size = w * h * 3
    rec = [None] * 4
    last = None
    checked = time.monotonic()
    with torch.no_grad():
        for k in range(n):
            raw = read_frame(inp, size)
            if raw is None:
                if last is None:
                    print("matte_gpu: no camera frames", file=sys.stderr)
                    return 1
                raw = last
            last = raw
            x = torch.frombuffer(raw, dtype=torch.uint8).to(dev).view(h, w, 3).permute(2, 0, 1)[None]
            x = x.float() * (1 / 255)
            _, pha, *rec = net(x, *rec, ratio)
            if k >= skip:
                a = (pha[0, 0] * 255 + 0.5).clamp(0, 255).to(torch.uint8)
                out.write(a.cpu().numpy().tobytes())
            now = time.monotonic()
            if gpu and now - checked > 1.0:
                checked = now
                v = vram_mb()
                if base is not None and v is not None and v > base + limit:
                    print(f"matte_gpu: GPU memory {v} MB > {base} + {limit} MB: stopping", file=sys.stderr)
                    return 99
                if pugcam_live():
                    print("matte_gpu: pugcam went live: leaving it the GPU", file=sys.stderr)
                    return 98
    out.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(1)
