# pugrec

Record a product demo in one go, edit it afterwards with full freedom.

`pugrec record` captures the screen at native resolution, the webcam and the mic, **and a
timeline of what happened**: cursor, clicks, key presses, window positions and focus.
Nothing is burned in. Then a **scene script** (`edit.yaml`) says what the viewer sees, scene by
scene - the camera, a window (followed as it moves, or part of it), the mouse, a split, with
blurred backgrounds where shapes don't fit - and pugrec previews it live in the browser and
builds 16:9 and 9:16 MP4s from the same script. See [EDIT.md](EDIT.md).

Built for PugPal (Hyprland on Fedora), main-pc's 5120x2160 screen in particular.

## Workflow

```bash
bin/pugrec record                 # overlay "Starting camera…" -> 3-2-1 -> go; Ctrl+C / `pugrec stop` ends
bin/pugrec record --keys          # also key presses: asks for your password first
                                  #   on the overlay: click the circle to pause, Cancel to discard
S=~/Videos/PugRec/2026-10-13_14-38-12
bin/pugrec audio $S               # mic-clean.flac: noise/keyboard/breaths out, -16 LUFS
bin/pugrec transcript $S          # transcript.jsonl: words + times, on the GPU (captions, smart cuts)
bin/pugrec draft $S               # edit.yaml: a first edit from focus changes, clicks, the transcript
bin/pugrec preview $S             # browser preview; edit edit.yaml, save, it updates
bin/pugrec build $S               # build-main.mp4 (1920x1080) + build-vertical.mp4 (1080x1920)
bin/pugrec whatsapp $S/build-vertical.mp4   # a copy WhatsApp plays inline (H.264 Main, no B-frames, < 16 MB)
```

Other commands: `record --no-input` (no keys, no clicks), `toggle` (for a keybind),
`info SESSION`, `check SESSION` (validate an edit, list scenes), `faces SESSION`,
`proxy SESSION`, `transcript --cuts` (print suggested cuts). `render` is an old auto-zoom test harness.

## Setup

Recording needs nothing beyond the system. Post-processing needs a venv, and transcription a
one-time GPU build of whisper.cpp (in a distrobox; the host stays untouched):

```bash
bin/pugrec setup                  # .venv (only when requirements changed) + the whisper model; idempotent
scripts/build-whisper.sh          # only if no whisper-cli on PATH (the PugPal image ships one)
scripts/build-gpu.sh              # the GPU build compositor (~5x faster builds); optional
```

Models and the DeepFilterNet binary are fetched into `~/.cache/pugrec` and checked against
pinned SHA-256s.

## What it records

- **Screen**: wf-recorder, AV1 on the GPU (VAAPI), 60 fps cap, native resolution (the GPU's
  H.264 encoder tops out at 4096 px). The mic goes into the same file as lossless FLAC; on main-pc
  it's PodMic RAW (`~/.config/pugrec/config.toml`) and is cleaned up afterwards. A muted mic
  (main-pc's `hotmic`) is warned about, never unmuted.
- **Webcam**: ffmpeg, MJPEG -> AV1 on the GPU, 1080p, synced by the kernel's frame timestamps.
- **Webcam**: the real camera - the BRIO by name, else the first hardware camera; never a virtual
  (v4l2loopback) one, even if that's the desktop's default (`camera = "<path or name>"` in
  `~/.config/pugrec/config.toml` to choose). For the recording its zoom/pan/tilt are set wide and
  centred (pugrec frames the face itself) and put back afterwards (`[camera_controls]` to change,
  `camera_controls = false` to leave the camera alone).
- **Cursor** and **windows** from Hyprland IPC.
- **Mouse buttons and wheel** by default, read without root (the desktop lets your own session
  read the mouse), so there's no password prompt. **Keys** only with `--keys` (or `keys = true` in
  `~/.config/pugrec/config.toml`): a small root helper (`pkexec`, this recording only, read-only,
  security keys skipped), so you type your password. Key codes are redacted while
  1Password/pinentry/polkit is focused or the screen is locked.

The session format is in [FORMAT.md](FORMAT.md); the edit script in [EDIT.md](EDIT.md).

## Output

H.264 (yuv420p) + AAC in MP4 with faststart: plays everywhere (browsers, WhatsApp, Slack,
YouTube).

Builds paint on the GPU when `scripts/build-gpu.sh` has built `pugrec-gpu`: the 5K recording is
decoded, composited and encoded without leaving the GPU, about 2x real time (a 4-minute demo in
~2 minutes; the CPU compositor takes ~10). It paints the same plan as the CPU compositor, to
within a few levels per pixel. Options:

- `--encoder x264` (also implied by `--hq` or `--preset`): x264 instead of the GPU's H.264
  encoder - ~15% smaller files for the same quality, ~40% slower. The GPU encoder uses
  qp = `--crf` + 4 (CRF 18 by default).
- `--compositor cpu`: the old CPU compositor, in parallel chunks (`--jobs`, default 4), x264.
  A GPU build that fails falls back to it by itself.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

## License

MIT - see [LICENSE](LICENSE). Models and tools fetched by `pugrec setup` (whisper.cpp, DeepFilterNet,
Silero VAD, YuNet) keep their own licenses.
