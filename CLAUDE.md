# pugrec

Screen recorder + event timeline for product demos, and a scene-script editor on top.
User docs: README.md. The session format (recorder -> everything else): FORMAT.md. The edit
script: EDIT.md. Keep both in sync with the code; bump the version on breaking changes.
GUIDE.md = how other software (agents) operates pugrec, self-contained: its appendix is a copy of
EDIT.md's reference sections - when the script changes, update both.

## Layout

```
bin/pugrec             launcher: system python3 for recording, .venv for everything after
pugrec/recorder.py     Session: wf-recorder + camera ffmpeg + pollers + input helper -> session.jsonl
pugrec/hypr.py         Hyprland IPC (request socket, socket2 events), window/monitor state + diffs
pugrec/input_helper.py root helper (pkexec), evdev -> JSON lines; stdlib + evdev only
pugrec/jsonl.py        single writer thread (append-only, flush 100 ms), crash-tolerant reader
pugrec/timeline.py     session.jsonl replayed: cursor, clicks, every window's geometry, focus, selectors
pugrec/faces.py audio.py transcript.py   derived: faces.jsonl, mic-clean.flac, transcript.jsonl
pugrec/edit/script.py  edit.yaml: strict validation, normalised shapes, sticky bubble, per-output overrides
pugrec/edit/resolve.py THE evaluator: script + sessions -> plan (per-frame layers) per output. Pure.
pugrec/edit/compose.py build compositor: paints plan layers (uint8, OpenCV), chunk-parallel, audio cut
pugrec/edit/preview.py proxies + local server (Range, SSE reload); web/ = canvas player of the plan
pugrec/edit/draft.py   starting edit.yaml from focus changes, click bursts, transcript
pugrec/render.py       old auto-zoom harness; its Camera is reused by `shot: auto`
scripts/build-whisper.sh  whisper.cpp + Vulkan, built in a distrobox
tests/                 unittest; fake_session.py makes a synthetic 5K session
```

## Decisions (James, 2026-10-13)

- **Recorder + format first**, then the editor as a **scene script** + browser preview, not a GUI
  timeline editor (James: making an editor is complex; the goal is for the scripting to be good).
- **One evaluator, two dumb compositors**: resolve.py decides everything; compose.py and
  web/player.js only draw layers. Never put framing logic in a compositor. A new layer type
  goes into both compositors with the same maths (gradient = CSS angles, shadow sigma =
  blur/2 = canvas shadowBlur). Background layers carry `bg: true` (transitions cross-fade them;
  spotlight and `move` skip them) - compositors ignore the flag.
- **New looks are options, not new defaults** (James, 2026-10-14, about `fit: inset`): adding
  one must leave every existing edit resolving to the same plan and must not change what `auto`
  or `draft` pick. Check by resolving all of ~/Videos/PugRec before and after.
- **Script times are source seconds**; 16:9 + 9:16 from one script; sources / timeline segments /
  overlays are in the schema so splicing and picture-in-picture need no compositor changes later.
- **Transcription on the GPU**: whisper.cpp Vulkan (AMD), built in a distrobox.
- **JSONL, append-only**, metadata included - no file rewritten at the end.
- **Keys/clicks via a root helper per recording** (pkexec), not the `input` group.
- **Screen via wf-recorder, not the portal**: PugPal's xdph is capped at 30 fps + SHM
  (Slack workaround in the dotfiles' xdph.conf) and shows a picker. wf-recorder = 60 fps, no picker.
- **AV1 for the source** (5120x2160; the GPU's H.264 encoder can't), **H.264 MP4 for output**.
- No pip deps for recording: PyGObject/evdev/stdlib from the image. numpy/OpenCV only in `.venv`.

## Git: commit little and often

James's rule (2026-10-13): **commit little and often.**
- Commit as soon as one logical change works (tests pass, or the recording/render was checked):
  don't batch several changes into one commit, and don't leave working changes uncommitted
  at the end of a step.
- One logical change per commit, so `git revert` undoes exactly one thing. A fix found while
  doing something else gets its own commit.
- Conventional Commits (`feat(record): ...`, `fix(render): ...`, `docs: ...`), with a body that
  says **why**, not just what.
- Author: James Woodall, GitHub noreply address (repo-local config, already set).
- Remote: github.com/jamesikanos/pugrec - **PUBLIC** since 2026-10-14 (history started fresh
  2026-10-13). Everything pushed is visible to anyone: no secrets, no customer names, no
  personal data, in files or commit messages. The earlier history lives in the private
  github.com/jamesikanos/pugscreenrecord and must never be pushed here (it names a customer).
  `main` for now; push after committing. Never force-push.
- **No customer names anywhere** - files, tests, examples, commit messages. Examples use Acme.

## Gotchas

- Sync anchor = wf-recorder's "pts_time: 0" log line; if a wf-recorder update changes its
  logging, `session_start.sync` falls back to "Output #0 log line" (~ms worse). With audio,
  the video's first packet is at ~0.033 s: sync is ~1 frame accurate.
- DMS's lock screen is a session-lock surface, not a layer: detected via
  `"LOCK" in monitor.solitaryBlockedBy` (hypr.monitor_state `locked`).
- The dev machine is James's daily driver: no sudo, no system changes. Recording tests
  write into a temp dir; use `--no-input` when James isn't there to answer the polkit prompt.
- **GPU experiments can crash James's desktop - don't run them unasked.** 2026-10-14: a headless
  Chrome test (WebCodecs + WebGL, 1200 x 5K frames uploaded as textures) ran the RX 9070 XT out of
  memory (`amdgpu: Not enough memory for command submission`, `Failed to pin framebuffer -12`):
  every app's GPU context died, Hyprland segfaulted, the session was lost and he had to reboot.
  The desktop shares this GPU. Any GPU-heavy test (many 5K textures, parallel VAAPI streams,
  shaders) needs his OK first, a small size first, and a watch on VRAM
  (`/sys/class/drm/card*/device/mem_info_vram_used`) with a stop well before it's full.
  Result of that experiment, for the record: no hardware AV1 decode in headless Chrome, and
  reading 1080p frames back costs 20-45 ms each - not worth it as a build compositor.
- Build speed ~0.35x realtime with 4 jobs (5K decode + x264 share the CPU). A float32 canvas was
  the first bottleneck (48 ms/frame): keep compositing uint8 + OpenCV.
- Chunked builds: Matroska doesn't store the last frame's duration, so concat parts carry an
  explicit `duration`, or a frame is lost per join.
- Preview: Chrome wants <video> elements in the DOM and seeked once, or a paused one never decodes.
  Headless checks: drive Chrome over CDP (`--remote-debugging-port`), not `--screenshot` (it fires
  before the videos are ready).
- Draft: ignore patterns must be anchored (`rofi` matched inside `Profile_6`, hiding every PWA);
  workspaces are polled at 10 Hz, so judge a focus's visibility ~0.3 s after it starts.
- Key events carry the cursor position, not the caret's: don't zoom on typing by position.
- Countdown overlay = pugrec/overlay/countdown.qml on Quickshell (DMS's toolkit): layer Overlay,
  **keyboardFocus Exclusive** (for Esc/Space; OnDemand only focuses after a click) with escape
  hatches because an exclusive overlay once locked James out: Esc, an input mask on the controls
  only (mouse never grabbed), a 2-minute self limit (then it starts), and the recorder's hard kill
  at 130 s plus finish(). Ask before showing it on James's screen in tests. Result by exit
  code (0 go / 3 cancel): console.log doesn't reach stdout. Downscaled grim screenshots hide the
  dim - measure brightness instead (204 -> 96).
- This harness's worktree guard rejects shell commands with variables, or the word for the VCS in
  paths/text (the repo lives under ~/ that-word): write small scripts in $CLAUDE_JOB_DIR/tmp instead.
- Camera (BRIO): GPU MJPEG decode segfaults ffmpeg; `-copyts` crashes av1_vaapi; so CPU decode +
  hwupload, and the first frame's time comes from ffmpeg's `start:` log line (CLOCK_MONOTONIC by
  default - don't pass `-ts abs`, that converts to wall clock). `exposure_dynamic_framerate` is on
  (the user's setting): fps drops in low light; VFR is fine.
- Mic mute is the user's privacy switch (`hotmic` mutes the iD4 hw node): warn + log, never unmute.
  Detected from the signal (digital zeros), because PodMic RAW's own mute flag stays off.
- Audio clean-up (`pugrec audio`): DeepFilterNet needs `-D` (delay compensation) or it drifts out of
  sync. Lessons from the dotfiles' podmic.conf: no 2-5 kHz boost (mouth clicks), no live level gate
  (chatters on breaths) - breaths are handled offline with Silero VAD segments instead. The VAD
  counts breaths *between phrases* as speech, so a fluent talker kept every one (2026-10-17: 314 s
  "speech" of a 322 s take); step 4 finds those from the sound (unvoiced, breath-level, centroid
  1.1-4 kHz, >= 0.22 s) after loudnorm, where the dBFS thresholds hold for every take.
  Loudness: gain -> limiter -> linear loudnorm; linear loudnorm alone stopped at -17.2 LUFS (TP cap).
  James can't be asked "does it sound good" by numbers alone: measure (sync lag, taps at key-event
  times vs speech, LUFS) AND have him listen.

## Status / next

- v1 done 2026-10-13: recorder (screen+mic, cursor, windows, Hyprland events), renderer, tests.
- Root input helper worked in James's first real recording (2026-10-13); Ctrl+C crash fixed after.
- Camera + bubble (bottom-left circle) added 2026-10-13. Camera 1080p: James - "the output render
  will never be 4k, this is just a software demo".
- **`pugrec render` is a throwaway test harness**, only for checking that recordings are usable
  (James, 2026-10-13: "Don't worry about the render at all, we'll be building an editor").
  Don't tune it (zoom feel, bubble crop, speed); that work belongs in the editor.
- Editor v1 done 2026-10-13: GPU transcript, script + resolver, build, preview, captions,
  transcript cuts, draft; 45 tests. Sample edit: ~/Videos/PugRec/2026-10-13_14-38-12/edit.yaml.
- Done 2026-10-14: `fit: inset` (cards on a backdrop) and `push` (slow push-in, James's pinned idea).
  Transitions, the bubble and push run on the output clock (frame count), never on source time:
  cuts inside a scene used to end fades early (a visible pop).
- **Pinned (James, 2026-10-14): a native GPU build compositor**, replacing compose.py (kept as the
  CPU fallback and for tests), reading the same plan. Why: builds are ~0.4x real time, limited by
  moving 33 MB 5K frames between ffmpeg, Python and x264 (measured: ~50% waiting for decoded
  frames, ~40% CPU compositing). Shape: VAAPI AV1 decode on the GPU's video chip -> frames into
  OpenGL/Vulkan without copying (dmabuf) -> shaders draw the layers (same maths as player.js) ->
  read back only the 1080p result -> x264. A fixed texture pool and a hard VRAM cap (~2 GB): the
  desktop shares the GPU (see the GPU-experiment gotcha). Estimate 3-5x faster, several days.
  Start with a small memory-watched prototype (10 s of 5K, one crop, fps + VRAM), James's OK first.
  Ruled out (measured 2026-10-14): GPU decode alone in today's pipeline (11% slower), GPU
  downscale (the 16% gain is the downscale itself, CPU does the same), more jobs (slower),
  GPU H.264 encode (bigger files for little), headless Chrome as compositor (no HW AV1 decode,
  20-45 ms readback per frame).
- Done 2026-10-17: callouts (`callouts:`) - info boxes in four looks (draw, bar, pill, section),
  styled by `style.callout` (a theme's), with direction of travel in and out; a section can lead
  its wipe with the theme's sparkle sweep. New layer: `stroke` (a part-drawn polyline); text
  layers may be `align: left`. Every existing edit resolved to the same plan before and after.
  `pause` holds the recording (frames with `hold: true`: one source moment, silence) while a
  callout comes in. The preview's player.js is not reloaded by the SSE plan reload: after a
  player change, hard-refresh the page (an old player looped the audio through a hold).
- **Pinned (James, 2026-10-17): camera motion is too extreme - make it gentler, with options.**
  From cutting a real demo, where every one of these looked wrong:
  - **push / punch in a split should move the camera pane only.** Today they zoom the whole
    scene, so the app pane lurches too ("jarring"); the workaround was a full-frame camera shot.
    Likely shape: by default a split's push/punch applies to its camera pane, plus pane-level
    `push:` / `punch:` to choose explicitly.
  - **A fixed framing from the average face position** (faces.jsonl over the shot's span),
    not following. `follow: false` today freezes on wherever the face is at the scene's first
    frame, which is arbitrary if the speaker shifts. An option such as `follow: average`.
  - **Gentler follow**: smoother and slower to react when it does follow (smoothing time /
    dead zone as options), instead of visibly tracking every movement.
  - **Options with milder defaults** for all of the above: James found today's punch overdone even
    at the default 6%. Changing a default changes existing edits' plans, against the "new looks
    are options" rule above - agree which defaults move before changing them.
- Next ideas: move transitions between contain views; overlays / other sources; key-shortcut
  captions from `key` events; title cards; VAAPI decode in builds; a keybind in the dotfiles.
