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

## macOS (Apple Silicon)

The same commands work on a Mac. `pugrec record` runs a small Swift helper (`macos/`,
built by `pugrec setup`): ScreenCaptureKit for the screen, AVFoundation for the camera, a
listen-only event tap for clicks and keys, the window list for windows and focus. Sessions go
to `~/Movies/PugRec`, as `screen.mov` (HEVC + the mic as ALAC) and `camera.mov` (HEVC):
Apple Silicon has no AV1 encoder. The session format is otherwise the same.

With [Homebrew](https://brew.sh) installed:

```bash
git clone https://github.com/jamesikanos/pugrec.git && cd pugrec && bin/pugrec setup
bin/pugrec panel
```

`setup` installs what's missing (the Xcode Command Line Tools; from Homebrew Python 3.14, ffmpeg,
whisper-cpp, node and the IBM Plex fonts), then builds macos/pugrec-capture, .venv, the whisper
model and the panel. Run it again after a pull: it only redoes what changed.

**Control panel** (`control/`, `pugrec panel`): a Stream Deck-style window of big keys for
record/stop, screen, mic, camera and keys, and the pipeline on the latest session (clean audio,
transcript, draft, preview, builds, WhatsApp copy, folder, edit script). It keeps itself out of
the recording.

Right-click (or long-press) Screen, Mic, Camera or Session to choose; the gear holds the countdown,
the transcript vocabulary and "keep on top". Started from the panel, macOS asks for permissions in
*Electron's* name, separately from the terminal's.

Permissions, once, for the app you record from (your terminal): **Screen Recording**, **Camera**,
**Microphone**, and **Input Monitoring** for `--keys`. macOS also asks now and then to let it
"bypass the system private window picker": allow it (it can appear over the countdown).
No Spaces API: a window is on screen (workspace 1) or not (-1). Window classes are bundle IDs
(`com.google.Chrome`), and focus is the frontmost app's frontmost window.

## Setup

Recording needs nothing beyond the system. Post-processing needs a venv, and transcription a
one-time GPU build of whisper.cpp (in a distrobox; the host stays untouched):

```bash
bin/pugrec setup                  # .venv (only when requirements changed) + the whisper model; idempotent
scripts/build-whisper.sh          # only if no whisper-cli on PATH (the PugPal image ships one)
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

H.264 (yuv420p, CRF 18) + AAC in MP4 with faststart: plays everywhere (browsers, WhatsApp,
Slack, YouTube). Builds run in parallel chunks (`--jobs`, default 4).

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

## License

MIT - see [LICENSE](LICENSE). Models and tools fetched by `pugrec setup` (whisper.cpp, DeepFilterNet,
Silero VAD, YuNet) keep their own licenses.
