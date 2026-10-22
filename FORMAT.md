# pugrec session format (version 1)

A session is a folder, by default `~/Videos/PugRec/<YYYY-MM-DD_HH-MM-SS>/`:

| File | What |
| --- | --- |
| `session.jsonl` | the timeline: metadata and every event, append-only |
| `screen.mkv` | screen video, AV1 (VAAPI), native resolution, variable frame rate (frames only when the screen changes), plus the mic as an Opus track |
| `camera.mkv` | webcam, AV1, 1920x1080 @ up to 30 fps (the BRIO lowers it in low light), PTS from 0 = its first frame; no audio |
| `screen.mov`, `camera.mov` | **macOS** instead of the two above: HEVC (no AV1 encoder on Apple Silicon), the mic as ALAC (lossless). The mic track may start ~0.15 s after the video: extracts pad it back to 0. Readers take the names from `stream_start.file` |
| `capture.log` | **macOS**: pugrec-capture's log, instead of `wf-recorder.log` / `camera.log` |
| `wf-recorder.log`, `camera.log` | the capture tools' logs, for debugging |
| `faces.jsonl` | **derived** (`pugrec faces`, recomputable): the webcam face track, see below |
| `transcript.jsonl` | **derived** (`pugrec transcript`): a `meta` line (model, backend, speed, `vocabulary` it was given), then `segment` lines `{t, end, text, words: [{t, end, w, p}]}`, ns on the session clock |
| `edit.yaml` | the scene script ([EDIT.md](EDIT.md)) - by hand, by Claude or by `pugrec draft` (`edit.draft.yaml` if one exists) |
| `proxy/` | **derived** (`pugrec proxy` / `preview`): browser copies of screen and camera, 30 fps CFR, times = source times |
| `build-<output>.mp4` | `pugrec build` results |
| `mic-clean.flac` | **derived** (`pugrec audio`): the mic cleaned up (noise, keyboard, breaths, tone, -16 LUFS). Mono 48 kHz 24-bit, **same start and length as the mic track in `screen.mkv`**, so it replaces it 1:1 |

The mic track itself (in `screen.mkv`) is raw and lossless (FLAC) from v1.1 on: on main-pc
it's PodMic RAW (`~/.config/pugrec/config.toml`), no processing at record time.

## faces.jsonl (derived)

One line per sampled camera frame (10 fps) that has a face; frames without one are absent:
`{"t": <session ns>, "x": .., "y": .., "w": .., "h": .., "score": ..}` - the largest face's box
as **fractions of the camera frame** (0..1, top-left origin), `t` on the session clock. Written
whole and swapped in (it's not a recording, so not append-only); delete it to recompute.

## session.jsonl

One JSON object per line. The file is **only ever appended to** and flushed about
every 100 ms, so a crash leaves a valid file up to that point. Readers must:

- **skip lines that don't parse** (a crash can leave a truncated last line);
- **sort by `t`** - sources (cursor poller, window poller, input helper, Hyprland
  events) write independently, so lines can be slightly out of order;
- treat a missing `session_end` as "crashed or still recording".

### Time

Every record has `t`: **integer nanoseconds since the screen video's PTS 0** (its first
frame). To find the video frame for an event, seek the video to `t / 1e9` seconds.
Accuracy: within about one frame (16-33 ms). The anchor is the moment wf-recorder
logs its first frame, measured in CLOCK_MONOTONIC (`session_start.sync` says how it
was found).

### Coordinates

`cursor`, window `at`/`size` and monitor `x`/`y` are **global compositor (logical)
coordinates**, as Hyprland reports them. Screen video pixels:
`px = (x - logical[0]) * scale`, `py = (y - logical[1]) * scale`, using the screen
`stream_start` record. (On main-pc: scale 1, monitor at 0,0, so they're equal.)

### Records

| `type` | Fields | When |
| --- | --- | --- |
| `session_start` | `format` (1), `started` (ISO wall clock), `host`, `monitors` [monitor state], `windows` [window state], `focus` (address), `sync` | first line |
| `stream_start` | `stream` ("screen"), `file`, `monitor`, `logical` [x, y, w, h], `pixels` [w, h], `scale`, `fps` (cap), `codec`, `audio` (source name or null), `cursor` ("embedded") | once the stream runs |
| `stream_start` (camera) | `stream` ("camera"), `file`, `device`, `pixels`, `fps`, `codec`, **`first_frame_t`** (ns): camera PTS 0 is at this session t. Its own `t` is the same. `controls`: the camera's zoom/pan/tilt during the recording. | when the camera's first frame arrives (~1.2 s in: BRIO warm-up) |
| `marker` | `name` ("start"), `countdown` (s), `warmup` (s from the key press to the countdown: password + camera start), `countdown_s` (s it was on screen; more than `countdown` = paused) | `start`: the end of the recorder's 3-2-1 countdown. Before it the files hold only the camera warm-up and the countdown overlay (the password prompt now comes before the screen recorder starts): edits begin here by default. A recording stopped before this marker is discarded |
| `mic_mute` | `source`, `muted` (bool or null = unknown) | at start, then on every change (polled 1 Hz). Muted = the audio track is silence: the recorder never overrides the user's mute |
| `cursor` | `x`, `y` | polled at 120 Hz, written only when it moved |
| `button` | `code` (`BTN_LEFT`, `BTN_RIGHT`, `BTN_MIDDLE`, ...), `value` 1 down / 0 up | from the input helper |
| `wheel` | `code` "vertical"/"horizontal", `value` (steps, sign = direction) | from the input helper |
| `key` | `code` (`KEY_A`, ... evdev names), `value` 1 down / 0 up / 2 repeat, `mods` (["ctrl", "shift", "alt", "super"] held), or `redacted: true` instead of `code` | from the input helper |
| `input_devices` | `devices` [names], `keys` (true = root helper, keyboards included; false = without root: what the session may read, usually just the mouse) | input helper started |
| `window_open` | window state | new window (10 Hz poll) |
| `window_update` | `address` + only the changed fields | moved, resized, retitled, ... |
| `window_close` | `address` | gone |
| `window_focus` | `address` (or null) | focus changed (Hyprland event) |
| `monitor` | `name` + changed fields of monitor state | workspace switch, lock, ... |
| `hypr` | `event`, `data` | every raw Hyprland socket2 event (layers, workspaces, ...), for anything not modelled above |
| `notice` | `message` | something the editor should know ("keys/clicks unavailable: ...") |
| `session_end` | `duration` (ns), `counts` {type: n} | last line, clean stop only |

**Window state:** `address`, `class`, `title`, `at` [x, y], `size` [w, h], `workspace` (id),
`floating`, `fullscreen` (0/1/2), `monitor` (index), `mapped`, `hidden`, `pid`.
A window is visible when its `workspace` is its monitor's `workspace` or `special`. A window that isn't visible has an
empty `title` (privacy: other workspaces aren't in the picture); it comes back when it is.

**Monitor state:** `name`, `x`, `y`, `width`, `height` (pixels), `scale`, `transform`,
`workspace` (active id), `special` (special workspace id, 0 = none), `focused`, `locked`.

### Redaction

Key codes are replaced by `"redacted": true` (timing kept, so "typing" still shows)
while the screen is locked or the focused window's class/title matches
`1password|pinentry|polkit|gcr-prompter|keyring|seahorse`. Modifier keys are kept.

### Compatibility

New record types and fields may appear in later versions; readers ignore what they don't
know. A breaking change bumps `session_start.format`.
