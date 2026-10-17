# Operating pugrec - a guide for agents

pugrec records product demos on the author's PugPal desktop (Fedora + Hyprland, a 5120x2160
monitor) and turns them into edited videos. This guide is for software (an AI agent, a script)
that operates it on the author's behalf. It says what to run, in what order, what every step
produces, how to check the result, and which steps need a human.

- Repo: `~/git/pugrec`. Run everything as `pugrec <command>` (on PATH: `~/.local/bin/pugrec`
  links to the repo's `bin/pugrec` launcher, which picks the right Python; the post-processing
  commands need `.venv`, already set up). Relative paths are taken from your current directory.
- This guide is self-contained: recording (sections 3-4), editing (5-7, and the complete
  `edit.yaml` reference in the appendix), building and checking (8-9). In the repo, `EDIT.md`,
  `FORMAT.md` (every data record) and `README.md` have more background.

## 1. Rules

1. **Recording needs the author.** They have to be at the computer: they do the demo, and key capture
   (`--keys`) asks for their password. Don't start a recording on your own initiative.
2. **No system changes.** No `sudo`, no package installs, no edits outside the repo and the
   session folders, no killing processes you didn't start. If something seems to need one, ask.
3. **Don't open browser tabs or pop things up on their screen unasked.** Use `preview --no-open`
   when you only need the server, and tell the author the URL.
4. **Never delete recordings** (`~/Videos/PugRec/*`). Derived files (listed below) can be
   regenerated; `screen.mkv`, `camera.mkv` and `session.jsonl` can't.
5. **Don't overwrite an `edit.yaml` you didn't write** without reading it first. `pugrec draft`
   never overwrites one (it writes `edit.draft.yaml` instead).
6. **Verify, then report.** Every step below has a check. Report numbers you measured, not
   expectations.

## 2. A session folder

Each recording is a folder `~/Videos/PugRec/YYYY-MM-DD_HH-MM-SS/` (the newest one is the
latest recording: `ls -t ~/Videos/PugRec | head -1`).

| File | Made by | What |
| --- | --- | --- |
| `screen.mkv` | `record` | 5120x2160 AV1 screen video, plus the mic as lossless FLAC (raw, unprocessed) |
| `camera.mkv` | `record` | webcam, 1080p AV1 |
| `session.jsonl` | `record` | the timeline: cursor, clicks, keys, windows, focus, workspaces, markers (FORMAT.md) |
| `mic-clean.flac` | `audio` | the mic cleaned up; the build uses it automatically |
| `transcript.jsonl` | `transcript` | words with timestamps |
| `faces.jsonl` | `faces` (build/preview make it on demand) | webcam face track |
| `edit.yaml` | you, the author, or `draft` | the scene script: what the final video shows (EDIT.md) |
| `edit.draft.yaml` | `draft` | a draft, when `edit.yaml` already exists |
| `proxy/` | `preview` / `proxy` | browser copies of the videos |
| `build-<output>.mp4` | `build` | the finished videos |

All times in these files are on one clock: **seconds (or ns) since the start of the screen
video**. `edit.yaml` uses the same seconds ("source seconds").

## 3. Recording (with the author)

```bash
pugrec record                    # in a terminal the author can see
```
What happens, in order (the author usually starts it with **Super+Alt+R** or the Stream Deck Rec key,
both `pugrec toggle`; a second press stops it):
1. Only with `--keys` (or `keys = true` in config.toml): the **password prompt** for key capture;
   the author types it, and nothing else starts before it. Without it there's no prompt: clicks are
   read without root.
2. The **overlay**: the screen dims, a spinner says "Starting camera…" while the screen and camera
   recorders start (Cancel / Esc works).
3. **3-2-1** in the same circle (Space or clicking the circle pauses; Esc/Cancel discards).
4. Recording (`● recording` in the terminal / `$XDG_RUNTIME_DIR/pugrec.log`). Stop: the same key,
   **Ctrl+C**, or `pugrec stop`.
Stopping or cancelling **before the countdown ends discards** the session (nothing is kept). After
it, everything is kept; edits start at the `start` marker (the end of the countdown). Loud
notifications warn if the mic is muted, the camera is busy (naming the program holding it), or the
screen video stalled (cursor kept moving after the last video frame).

Useful flags: `--keys` (also key presses; password prompt), `--no-keys` (overrides config),
`--no-input` (no keys, no clicks), `--countdown N`
(`0` = none), `--no-camera`, `--no-mic`, `--mic SOURCE`, `--dir DIR`.
For a test recording without the author (e.g. checking the pipeline): `--countdown 0` (no `--keys`)
into a temp `--dir`, and stop it with SIGINT (Ctrl+C) or `pugrec stop`.

If the mic is muted, a critical notification says so and the audio is silence. pugrec never
unmutes it (that's the author's privacy switch, the Stream Deck "Mic" key / `hotmic`).

**Check a recording:**
```bash
pugrec info SESSION
```
Look for: a `duration` (missing = it crashed or is still recording), `stream screen` and
`stream camera`, events including `marker 1`, `button` counts > 0 (clicks were captured; `key`
counts only with `--keys`), and in `session.jsonl` a `mic_mute` line with `"muted": false`. Mic level:
`ffmpeg -i SESSION/screen.mkv -map 0:a -af volumedetect -f null - 2>&1 | grep max_volume`
(around -10 dB with speech; -91 dB = silence, the mic was muted).

## 4. Post-processing, in this order

```bash
pugrec audio SESSION        # mic-clean.flac: noise, keyboard taps, breaths out; -16 LUFS. ~5 s per 20 s
pugrec transcript SESSION   # transcript.jsonl, on the GPU. ~10 s per 2 min. --cuts prints suggested cuts
                                #   --prompt "Acme, FieldPal" / --theme NAME: names to spell right
pugrec draft SESSION        # edit.yaml (or edit.draft.yaml): a first edit from the timeline
```
Run `audio` before `transcript` (the transcript is made from the cleaned audio). `faces` runs
by itself when a build or preview needs it: `build` first (~30 s for a 2 min camera), `preview` in
the background (the page works at once; camera shots re-frame on the face when it's done, the log
says `faces: ... -> faces.jsonl` and `resolved ... (v2)`).

## 5. Reading what happened

**What was said** - `transcript.jsonl`: a `meta` line, then one line per phrase:
```json
{"type": "segment", "t": 26440000000, "end": 29980000000, "text": "So here is the new settings page...",
 "words": [{"t": 26440000000, "end": 26640000000, "w": "So", "p": 0.91}, ...]}
```
`t`/`end` are nanoseconds; divide by 1e9 for source seconds. `p` is the confidence. Names are
often misheard (a product called **Acme** can come out as "Akme"). Prevent it at the source:
`pugrec transcript SESSION --prompt "Acme, FieldPal"` (or `--theme <name>` for the theme's
`vocabulary`; `vocabulary = [...]` in `~/.config/pugrec/config.toml` is always used) - re-run the
transcript if names came out wrong. Whatever is still wrong: `style.captions.fix` (multi-word keys
allowed, e.g. `"Field Pal": FieldPal`), rather than editing the transcript.

**What happened on screen** - `session.jsonl`, one JSON object per line (FORMAT.md has all of
them). The useful ones for editing:

| `type` | Tells you |
| --- | --- |
| `marker` (`name: start`) | where the demo starts (after the countdown) |
| `window_focus` (`address`) | which window had focus from this moment |
| `window_open` / `window_update` | a window's `class`, `title`, `at` (x, y), `size`, `workspace` |
| `button` (`value: 1`) | a click (`code`: `BTN_LEFT`, ...) |
| `key` | key presses (`code` or `redacted: true`) |
| `monitor` (`workspace`) / `hypr` (`event: workspacev2`) | workspace switches |
| `notice` | something went wrong (e.g. "keys unavailable") |

The draft's header lists every window as `address  class  title`; use those addresses in
`edit.yaml`. A small reader:
```python
import json
recs = sorted((json.loads(l) for l in open(f"{S}/session.jsonl") if l.strip()), key=lambda r: r["t"])
clicks = [r["t"] / 1e9 for r in recs if r["type"] == "button" and r.get("value") == 1]
switches = [(r["t"] / 1e9, r["workspace"]) for r in recs if r["type"] == "monitor" and "workspace" in r]
```
(Skip lines that don't parse - a crash can truncate the last one.)

## 6. Writing `edit.yaml`

Start from the draft, or from this skeleton. The full option list is in EDIT.md.

```yaml
version: 1
outputs:
  - {name: main, size: 1920x1080}
  - {name: vertical, size: 1080x1920}
timeline:                                  # what's kept, in order (source seconds). Or use `cuts:`, not both
  - {source: main, from: 9.6, to: 13.6}
  - {source: main, from: 26.2, to: 110.9}
style:
  spotlight: {window: {address: "0x5616f5aa83e0"}}   # the demo window sharp, the rest blurred
  captions: {fix: {Akme: Acme}}
  bubble: {corner: bottom-right}
scenes:                                    # each lasts until the next `at` (source seconds)
  - {at: 9.6, label: Intro, shot: camera}
  - at: 26.2
    label: The app
    shot: {window: {address: "0x5616f5aa83e0"}}
    transition: {fade: 0.5}
  - at: 30.3
    label: Logging in
    shot: {cursor: {zoom: 1.8}}
    transition: {move: 0.6}
    vertical: {shot: {cursor: {zoom: 1.3}}}   # a different shot on the 9:16 output only
```

Rules that matter:
- **Times are source seconds**, never output seconds. Scenes must be in increasing `at`.
- **Trimming:** `timeline` segments (keep these ranges) or `cuts` (remove these ranges, plus
  optional `pauses: 1.2` / `fillers: true` from the transcript). Not both.
- **Shots:** `camera`, `screen`, `auto`, `{window: SEL}` (+ `follow`, `region: [x, y, w, h]`
  as fractions of the window), `{cursor: {zoom}}`, `{rect: [...]}`, `{split: [A, B], ratio}`.
  Window selectors: `{address: "0x..."}` (exact - prefer it), `{class: regex}`, `{title: regex}`,
  `focused`.
- **Fit:** `auto` (default - leave it unless there's a reason), `cover`, `contain`, and `inset`
  (the picture as a rounded card on a backdrop; on a scene or on one split pane).
- **Transitions:** `cut`, `{move: s}` (the camera glides; needs one view on both sides, not a
  split, not screen <-> camera), `{fade: s}`.
- **The bubble** (webcam circle) settings stick until changed; it hides itself on camera shots.

**Recipes**
- *False start:* read the transcript, find where the good take begins ("let's restart",
  repeated sentences, a long pause after a mistake) and start a `timeline` segment just before
  that phrase's first word (`t / 1e9 - 0.2`). Keep a good opening line before the mistake as its
  own short segment if there is one.
- *Recorded "around" the demo* (start on one workspace, demo on another, come back): find the
  two `monitor` workspace switches in `session.jsonl` and keep only the stretch between them
  (+/- 0.1 s).
- *Scenes:* a scene where the story changes - a new topic in the speech, a new window, a burst of
  clicks. `camera` for the intro/outro, `window` when talking about an app, `cursor` around
  clicks (start ~0.5 s before the first click), `split` (window + camera) while waiting for
  something on screen and talking about it.
- *Presenter / card-on-a-stage look* - an option, not a default; use it when the author asks for that
  look ("like a YouTuber", "polished", "the camera in a card", "the app floating on a background")
  or it clearly suits the purpose, and keep everything else as you would anyway:
  - talking head beside the app: `split: [{window: SEL}, {camera: {}, fit: inset, inset: {aspect:
    "2:3"}}]` with `ratio: ~0.66`;
  - the app floating on the backdrop: `fit: inset` on a window scene (the window becomes the card);
  - intro/outro as a camera card: `shot: camera, fit: inset` (a `move` from a full-frame camera
    lifts it in).
  The backdrop comes from the theme (`style.backdrop`); without one it's the PugPal gradient. Keep
  `shadow` at 0 unless the theme or the author wants one. Look at a built frame: the card should have
  even room around it and nothing important cut off.
- *Slow push-in* - also an option, not a default: `push: true` on a scene that holds a while
  (a talking-head intro/outro, a long camera or window shot, an inset card) makes it feel alive.
  Skip it on short scenes (< ~3 s) and on cursor/auto shots. Use it when the author asks for motion /
  "Ken Burns" / "zoom in slowly", or with the presenter look above.
- *Punch-in* - an option for emphasis: `punch: <s>` on a camera scene, `at` on the stressed
  word of the point being made (from transcript.jsonl). Use few (one per ~30 s at most, or they
  stop meaning anything), on talking-head shots - not on the app, and never in a scene with
  `push` (drop the push). Use it when the author asks for
  emphasis / "punch in" / "zoom on that line".
- *Callouts* - an option: small animated info boxes (`callouts:`, appendix) for what the screen
  can't say - a role, what a feature is, what's next - and `look: section` headers over a tab
  switch or a load. Write them from the transcript and the work's history (commits, tickets),
  check every claim against the code, and keep them few (one per 30-45 s). A section over a
  scene change starts `in` before it; `pause` only where the speaker left a real break.
- *Vertical output:* give it tighter shots via `vertical:` overrides (cursor zoom 1.2-1.4).
- *For a customer / brand:* `style: {theme: <name>}` (`pugrec themes` lists them; an unknown name
  is an error listing the installed ones), plus `intro: {title, subtitle}` / `outro:` title cards
  in that theme's colours. Override single keys in edit.yaml's `style` if needed (appendix: Themes).

## 7. Validate

```bash
pugrec check SESSION        # exit 1 + "error: <where>: <what> (did you mean ...?)" if invalid
```
On success it prints each output's length and every scene with its source and output time,
plus warnings. Fix errors; read warnings (e.g. a `move` that had to become a fade, a window
selector that matched nothing). A scene shown as "not shown (cut)" lies entirely in a removed range.

## 8. Preview (for the author) and build

```bash
pugrec preview SESSION                 # browser page; updates whenever edit.yaml is saved
pugrec preview SESSION --no-open       # just the server; prints the URL (next free port from 8765)
```
The page plays the edit as it will be built: output tabs (main/vertical), scene list, scrub bar.
Links like `http://127.0.0.1:8765/#t=12.5&o=vertical` open at a time and output.

```bash
pugrec build SESSION                   # build-main.mp4 + build-vertical.mp4 next to edit.yaml
pugrec build SESSION --hq              # final export: CRF 14, preset slow (bigger, slower)
pugrec build SESSION --output main --from 4 --to 12    # a quick partial build (output seconds)
pugrec build SESSION --whatsapp        # also build-<output>-whatsapp.mp4 per output
pugrec whatsapp SESSION/build-vertical.mp4   # a WhatsApp copy of any MP4/MKV (~30 s), raw recordings too
```
**WhatsApp** plays a video inline only when it follows Meta's media rules: MP4, H.264 + AAC,
one audio stream, **no High profile with B-frames** (Android clients can't play it), moov at
the front, and **16 MB at most** (bigger files go as a document, with no player). Normal builds
are High with B-frames and often bigger than 16 MB, so they arrive as files. The WhatsApp copy
is H.264 Main 4.0 without B-frames, 30 fps, limited range tagged BT.709, AAC 96k, faststart,
with a two-pass bitrate aimed at 15 MB (stepping down to 720p/540p for longer videos; over
~7 min it refuses - cut it shorter). For a phone audience send the vertical one.
In WhatsApp attach it with **+ → Photos & videos**: "Document" always sends a file (no player).
Speed: ~0.4x real time with 4 jobs (a 90 s edit takes ~3.5 min per output). Output: H.264
MP4 + AAC, plays everywhere. For sharper text add an output like `{name: hd, size: 2560x1440}`
and build it with `--output hd`.

## 9. Verify a build

```bash
ffprobe -v error -count_frames -show_entries stream=codec_type,width,height,nb_read_frames:format=duration \
  -of compact SESSION/build-main.mp4
ffmpeg -v error -y -ss 30 -i SESSION/build-main.mp4 -frames:v 1 /tmp/frame-30s.png   # then look at it
```
Check: the duration matches `pugrec check`, both video and audio streams exist, and frames at
a few scene times show what the script says (the right window, the zoom on the click, the
caption text, the bubble corner, the rest of the screen blurred if the spotlight is on). If you
can't look at images, at least compare durations and audio levels.

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `info` shows no duration | the recorder crashed or is still running (`pugrec stop`) |
| Audio is silence (-91 dB) | the mic was muted while recording; nothing to recover, record again |
| `key` count is 0 | recorded without `--keys` (the default), or the author dismissed the password prompt (then only the mouse was captured) |
| No camera stream | camera unplugged / busy; `notice` lines in `session.jsonl` say which. Busy "in use by" a meeting/loopback camera program: that program holds the BRIO - pugrec records the real camera, never the loopback, so it has to let go of it (the author's call) |
| `preview`: address in use | another server on the port; pugrec moves to the next free port and prints it |
| `transcript`: whisper-cli missing | `scripts/build-whisper.sh` (builds it in a distrobox; the author's go-ahead first) |
| `check`: "no window matches" | the selector matched nothing at that scene's start: use an `address` from the draft header |
| Captions spell a name wrong | `style.captions.fix: {Wrong: Right}` |
| A notification popped up over the demo window | the spotlight can't hide things *on top of* the window: cut that moment or use another shot |

## Worked example: a product demo with a false start

The author started recording on workspace 1, did the demo on workspace 2, went back to 1 to stop,
and fumbled the first take. What was done:
1. `info`: 112.5 s, keys/clicks captured, mic live, `start` marker at 6.5 s.
2. `session.jsonl`: workspace 2 from 7.2 s to 111.0 s.
3. `audio`, `transcript`: the transcript showed the false start (a login that failed, then
   "Let's restart", 13.8-24.9 s) and where the good take began (26.4 s).
4. `edit.yaml`: segments 9.6-13.6 (the opening line), 26.2-33.0, 35.3-110.9 (a 2.3 s loading
   wait removed); scenes from the transcript and the click times (camera intro, the browser
   window, cursor zooms around clicks, a split while the app works); spotlight on the browser
   window; a caption fix for the product name.
5. `check` (no warnings), `preview` for the author, `build`: two 86.4 s videos.

## Appendix: the complete `edit.yaml` reference

So this guide is enough on its own. (A copy of EDIT.md's reference sections - EDIT.md is
the source; when one changes, update the other.)

### A complete example

```yaml
version: 1
outputs:
  - {name: main, size: 1920x1080}
  - {name: vertical, size: 1080x1920}
cuts:
  pauses: 1.2                 # cut silences > 1.2 s (from the transcript), keeping 0.3 s each side
  fillers: true               # cut um / uh / erm
  ranges: [[41.0, 44.2]]      # and these, by hand
style:
  bubble: {corner: bottom-left, size: 0.26}
  captions: {show: true}
scenes:
  - at: 0
    label: Hello
    shot: camera                                        # full-screen camera, face-centred
  - at: 4.2
    shot: {window: {class: google-chrome}, follow: true}  # a window, following it as it moves
    transition: {move: 0.8}
    bubble: {corner: bottom-right}                      # the camera bubble moves (and stays there)
  - at: 9.0
    shot: {window: {title: Grafana}, region: [0, 0.1, 0.5, 0.4]}   # part of a window
  - at: 14.0
    shot: {cursor: {zoom: 2.2}}                         # follow the mouse
  - at: 20.0
    shot: {split: [{window: {title: Grafana}}, camera], ratio: 0.65}
    transition: {fade: 0.4}
  - at: 26.0
    shot: auto                                          # automatic: zoom on clicks, else focused window
    fit: contain                                        # the whole view, blurred background around it
    vertical: {shot: {cursor: {zoom: 1.5}}}             # different shot on the vertical output only
```

### Top level

| Key | Meaning |
| --- | --- |
| `version` | `1` |
| `outputs` | `[{name, size: WxH, fps}]` - default `main` 1920x1080 @ 60. Names are used for per-output overrides (`vertical:` in a scene). Sizes must be even. |
| `cuts` | a list `[[from, to], ...]`, or `{pauses: s, fillers: bool, ranges: [...]}` (pauses/fillers need `transcript.jsonl`) |
| `timeline` | instead of cuts: explicit segments `[{source, from, to}]` in order (splicing; reordering) |
| `sources` | `{main: {session: .}}` (default). Other sources (sessions, videos, images) are planned. |
| `audio` | `{source: mic-clean | raw | none}` - mic-clean (from `pugrec audio`) if it exists, else raw |
| `style` | defaults, below |
| `scenes` | the list of scenes, by `at` |
| `intro` / `outro` | title cards before/after the video: `"Title"` or `{title, subtitle, duration}` |
| `callouts` | animated info boxes and section headers over the video: `[{at, title, text, look, ...}]` (Callouts, below) |
| `overlays` | reserved (picture-in-picture of other sources, later) |

### Scenes

Each scene lasts until the next scene's `at`.

| Key | Meaning |
| --- | --- |
| `at` | source seconds where it starts |
| `shot` | what to show (below) |
| `fit` | `auto` (default) / `cover` / `contain` / `inset` (below) |
| `background` | behind a `contain` view or an `inset` card: `blur` (contain's default), `backdrop` (inset's default: the style's backdrop) or a colour `"#16171a"` |
| `inset` | for `fit: inset`: `{padding, radius, aspect, position, shadow}` over `style.inset` (below) |
| `push` | a slow push-in while the shot holds: `true`, a number (`to`), `{to, over}`, or `false` (default: the style's, off) - below |
| `punch` | a quick punch-in on an important point: a source time (when the zoom lands), a list of them, or `{at, zoom, dip, hold}` items (default: none) - below |
| `transition` | into this scene: `cut`, `{move: s}`, `{fade: s}` (default `{move: 0.6}`) |
| `bubble` | camera bubble: `{corner, size, margin, shape, show}`, `false` to hide, `true` to show. **Sticky**: applies until a later scene changes it |
| `spotlight` | `false` / `true` / `{window, blur, dim}` for this scene only (default: the style's) |
| `label` | name in the preview/check (default: described from the shot) |
| `<output name>` | overrides for one output, e.g. `vertical: {shot: ..., fit: ...}` |

#### Shots

| Shot | What it shows |
| --- | --- |
| `screen` | the whole monitor |
| `camera` / `{camera: {zoom: 1.3}, follow: true}` | the webcam, head and shoulders, centred on the face (`faces.jsonl`). `follow` (default true) re-frames as the face moves; `follow: false` holds the framing where the face was when the scene started - steadier for a talking head, and with `push` a clean, slow push-in |
| `auto` / `{auto: {zoom: 2}}` | automatic: zooms in on clicks/typing/scrolling (slightly before they happen), otherwise frames the focused window |
| `{window: SEL, follow: true, region: [x, y, w, h], padding: 0.04}` | a window. `follow` (default true) tracks it as it moves/resizes; `follow: false` freezes it where it was when the scene started. `region` = part of the window, fractions of its size. |
| `{cursor: {zoom: 2, dead_zone: 0.2}}` | follows the mouse; the view only moves when the cursor leaves the middle `1 - 2*dead_zone` |
| `{rect: [x, y, w, h]}` | a fixed screen area, pixels - or fractions of the screen if all values <= 1 |
| `{split: [SHOT, SHOT], ratio: 0.5, layout: auto}` | two panes; `auto` = side by side on wide outputs, stacked on tall ones (`row`/`column` to force). A pane may carry its own `fit`, `inset` and `background` next to its shot (`{camera: {}, fit: inset}`); they win over the scene's |

**Window selectors** (`SEL`): `{class: regex}`, `{title: regex}`, both, `{address: "0x..."}` (exact; the draft
and `session.jsonl` list them), or `focused` / `{focused: true}` (whatever has focus, switching with it).
A class/title selector is resolved once, when the scene starts (several matches: visible, then focused,
then largest). If nothing matches, the scene shows the whole screen and `check` warns.

#### Fit: when the shape doesn't match the output

Every shot has a target rectangle; the output (or split pane) has its own aspect.
- `cover`: show more around the target until it fills the frame; if the screen runs out, crop.
- `contain`: show exactly the target, centred, with a **blurred, dimmed copy** of the same view
  behind it (or a colour: `background: "#16171a"`).
- `auto` (default): `cover` unless that would cut off more than 15% of the target, then `contain`.
  On a 5120x2160 screen, a 16:9 output of the whole screen is `contain`; on 9:16, most windows are.
- `inset`: the picture as a **rounded card with room around it, on a backdrop** (next section).
  Only when a scene or pane asks for it; `auto` never picks it.

#### Inset: a card on a backdrop

One more way to frame a shot - the "presenter" look of keynotes, Screen Studio and many YouTube
videos: the picture doesn't fill its pane, it sits as a card on a designed background. Use it
when an edit wants that look; it isn't a default and doesn't replace cover/contain/split.

```yaml
scenes:
  - at: 11.3                       # the app on the left, you as a portrait card on the right
    shot:
      split:
        - {window: {class: google-chrome}}
        - {camera: {}, fit: inset, inset: {aspect: "2:3", position: [0.5, 0.4]}}
      ratio: 0.66
  - at: 34.4                       # the whole app floating on the backdrop
    shot: {window: {class: google-chrome}}
    fit: inset
  - at: 60
    shot: camera                   # a camera card; with `transition: {move: s}` from a full-frame
    fit: inset                     # camera it lifts into the card while the backdrop fades in
```
- `inset` keys (scene, pane, or `style.inset` for the defaults): `padding` (room around the card,
  fraction of the pane's shorter side, 0.06), `radius` (corners, px at 1080p, 18), `aspect`,
  `position` ([x, y], where the card sits in the free room: 0 = left/top, 1 = right/bottom; [0.5, 0.5]),
  `shadow` (0 = none, the PugPal default .. 1 = strong).
- `aspect: auto` (default): the camera fills the room (face-centred); a window, cursor, rect or screen
  card takes the target's own shape, whole - a window *is* the card (no margin of desktop around it).
- `aspect: "2:3"` (or a number): the card has that shape and the view is cropped to it (the camera
  centred on the face for it). A portrait card beside the app is usually `"2:3"` or `"3:4"`.
- Behind the card: the style's `backdrop` (default), or `background: blur` / `"#rrggbb"`.
  `background: backdrop` also works for `fit: contain`.
- In a split only that pane is a card; the other pane is drawn as usual.

#### Push: a slow push-in while a shot holds

```yaml
  - at: 4.6
    shot: camera
    push: true                 # = {to: 0.8, over: 10}: the view tightens to 80% over 10 s
  - at: 30
    shot: {window: {address: "0x5616f59775b0"}}
    push: {to: 0.9, over: 6}   # gentler and quicker
```
The "Ken Burns" move: barely noticeable, but a long hold on a face or an app feels alive. It's
an option, off unless a scene (or a theme's `style.push`) asks; `push: false` turns a style's off
for one scene. It eases in over 1 s, moves steadily, eases out at `to` and then holds; a shorter
scene simply gets less of it. It follows the video as watched, so cuts inside the scene
(`pauses`, `fillers`) don't make it jump. It tightens about the view's centre - the face, for the
camera - and works full frame, in split panes (both panes), in `contain` views and inside inset
cards (the card stays, the picture tightens). Not on `cursor`/`auto` shots (they move already;
`check` warns). `push: true` is 0.8 on wide outputs and 0.9 on tall ones: the camera is 1080p and
a 9:16 frame already enlarges it ~1.8x, so a full push there gets soft (an explicit `to` is used as is).

#### Punch: a quick zoom in on an important point

```yaml
  - at: 4.6
    shot: camera
    punch: 12.4                                # the zoom lands at 12.4 s (source): put it on the word
  - at: 30
    shot: camera
    punch: [33.1, {at: 41.0, zoom: 0.15, hold: 3}]
```
The YouTuber's emphasis zoom: the view pulls back 0.6% (`dip`, the anticipation), snaps in 6%
(`zoom`) in 0.3 s with a slight overshoot, lands on `at`, holds `hold` s (1.5) and snaps back out
over 0.3 s. The move starts 0.45 s before `at`, so `at` is the stressed word in the transcript.
Defaults come from `style.punch: {zoom: 0.06, dip: 0.006, hold: 1.5}`; `dip: 0` = straight in.
Keep it subtle: 10% looked overdone on a talking head. It
runs on the video as watched (a cut inside it doesn't jump or end it); one whose start was cut
away still fires when the source is inside it. It works where `push` does (camera, window,
screen, rect; full frame, panes, `contain`, inset cards - the card stays). Not together with
`push` in one scene: the two moves fight (`check` warns).
A view already showing the whole source has no room for the dip, so it's skipped there. Not on
`cursor`/`auto` shots; `check` warns about those and about a punch after its scene ends.

#### Spotlight: one window sharp, the rest of the screen blurred

```yaml
style:
  spotlight: {window: {address: "0x5616f5aa83e0"}, blur: 40, dim: 0.35}
scenes:
  - at: 66.9
    spotlight: false          # this scene shows the whole desktop
```
Every view of the screen - window shots, cursor zooms, `auto`, `screen`, the screen pane of a
split - keeps the spotlit window sharp and blurs and dims everything else, following the window
if it moves. For a single-screen demo that's focus *and* privacy: other apps' text is unreadable.
`window:` takes the same selectors as shots (`address`, `class`, `title`, `focused` - which follows
focus). Per scene: `spotlight: false` / `true` / `{...}`, not sticky (the style applies otherwise).
Zoomed in inside the window, nothing is blurred. Limits: anything outside the window's rectangle
is blurred too (a menu or popup sticking out of it, the cursor when it leaves it), and anything
*over* the window stays sharp (e.g. a notification popping up on top of it). `pugrec draft` turns
it on for the window with the most focus time.

#### Transitions

- `cut` - instant.
- `{move: s}` - the virtual camera glides from the previous view to the new one (needs a single
  full-frame view of the same source on both sides; otherwise it becomes a fade and `check` says so).
- `{fade: s}` - crossfade.
- `{sparkle: s}` - a crossfade with 4-point star sparkles twinkling across the frame.
- `{sweep: s}` - the theme's sprite (`style.sparkle.image`) crosses the frame left to right,
  revealing the next scene behind it, leaving a sparkle trail.
`sparkle` and `sweep` are **a theme's signature, not a general effect**: they only work when the
theme (or the edit) has a `sparkle:` section in its style - anywhere else `check` refuses them.
They're deterministic, so the preview and the build show the same sparkles.
The bubble animates to its new corner/size with the transition (0.5 s on a cut).

### Style (defaults)

```yaml
style:
  theme: null                # a theme name or path (below); its keys are the base for everything here
  accent: "#ff6b2c"          # click rings, the spoken word in captions, the bar on title cards
  bubble: {shape: circle, size: 0.26, corner: bottom-left, margin: 0.04, show: true,   # size/margin: of the shorter side
           border: {color: "#f1e4d9", width: 3}}                                       # width px at 1080p; false = none
  click_rings: true          # a ring on each click, in the accent colour
  captions: {show: true, size: 0.045, position: auto, max_words: 6, highlight: true,   # auto = bottom / lower third on 9:16
             font: IBM Plex Sans, weight: 600, color: "#f1e4d9",                       # any installed font family
             background: "#16171a", background_opacity: 0.78,                           # the box behind the words
             fix: {}}                # {Misheard: Right}; keys may be several words ("Field Pal": FieldPal),
                                     # case-insensitive; edit.yaml's fix MERGES with the theme's
  logo: null                 # {file: logo.png, corner: top-right, size: 0.12, margin: 0.03, opacity: 0.9}
  card: {background: "#16171a", color: "#f1e4d9", font: null, duration: 3.0,         # intro/outro cards; font null = captions'
         image: null, image_size: 0.12,   # image: a brand icon, centred above the title; size = its height / the shorter side
         fade_in: null, fade_out: 0.4,    # seconds; fade_in null = 0 for the intro (frame 0 = thumbnail), 0.4 for the outro
         title_case: false}               # true: titles in Title Case ("decision and gated cards" -> "Decision and Gated Cards")
  spotlight: null            # {window: SEL, blur: 40, dim: 0.35}
  sparkle:                   # only for themes that want the sparkle/sweep transitions (opt-in)
    {colors: null,           # star colours; null = white + accent
     image: null,            # the sweep's sprite (PNG/JPEG/WebP, relative to the file naming it)
     image_size: 0.3,        # sprite height / the shorter side
     pixelated: false,       # true: nearest-neighbour scaling, for pixel art
     flip: false,            # mirror the sprite (it travels left to right)
     count: 36}              # stars per burst / trail
  background: "#16171a"      # behind splits
  blur: 40                   # px at 1080p, blurred backgrounds
  radius: 6                  # px at 1080p, rounded panes
  gap: 0.02                  # split gap
  padding: 0.04              # around a framed window
  backdrop:                  # behind inset cards (and `background: backdrop`)
    {gradient: ["#16171a", "#32231f", "#e8480f"],   # 2-4 colours: PugPal ink -> Jesse's mask -> FieldPal orange
     angle: 160,             # CSS degrees: 0 = towards the top, 90 = right, 180 = down
     image: null}            # or an image (PNG/JPEG/WebP, cover-cropped); setting one replaces the other
  inset: {padding: 0.06, radius: 18, aspect: auto, position: [0.5, 0.5], shadow: 0}   # fit: inset defaults
  push: null                 # e.g. {to: 0.8, over: 10}: every scene pushes in unless it says push: false
  punch: {zoom: 0.06, dip: 0.006, hold: 1.5}  # defaults for scenes' punch-ins
  callout: {look: bar, corner: top-left, margin: 0.045, size: 0.034, text_size: 0.024,   # callouts (below)
            padding: 0.02, font: null, weight: 700, text_weight: 500, color: "#f1e4d9",
            background: "#16171a", opacity: 0.86, border: null, border_width: 2, radius: 12,
            duration: 4.5, in: 0.7, out: 0.35, from: auto, to: none, distance: 0.025, sparkle: false,
            pause: 0}
  transition: {move: 0.6}    # default transition
```
The bubble hides automatically when the shot itself shows the camera. Captions appear when the
recording has `transcript.jsonl`; the word being spoken is highlighted. Colours are `"#rrggbb"`.
`logo.file` is PNG/JPEG/WebP (export SVGs first), relative to the file that names it; `size` is its
width as a fraction of the output width (the height keeps the image's aspect).

#### Themes: a customer's or brand's look

A theme is a YAML file of `style` keys (plus an optional `description`; `vocabulary:` - a list of
names for the transcript, used by `pugrec transcript --theme NAME`; and default `intro:`/`outro:`
cards), validated exactly like `style`. `style: {theme: acme}` loads `~/.config/pugrec/themes/acme.yaml` (or `theme: some/path.yaml`,
relative to edit.yaml). Order: pugrec's defaults (the PugPal look) <- the theme <- edit.yaml's own
`style` keys, key by key (`captions: {color: ...}` in edit.yaml changes only the colour). An unknown
theme is an error listing the installed ones. A brand's `backdrop:` (its gradient, or a stage image
next to the theme file) belongs in its theme, so every inset card in that brand's edits matches.
```bash
pugrec themes                # the installed themes and their descriptions
pugrec themes --defaults     # the PugPal look as a complete theme file (a starting point)
```
**Getting names right.** Best at the source: give whisper the names, and the transcript (and so
captions, cuts and drafts) spells them right - `pugrec transcript SESSION --prompt "FieldPal, Acme"`,
`--theme acme` (the theme's `vocabulary`), and/or `vocabulary = ["FieldPal"]` in
`~/.config/pugrec/config.toml` (always used). All three combine. `captions.fix` is the fallback for
what still comes out wrong.
```yaml
## ~/.config/pugrec/themes/acme.yaml
description: Acme Corp - blue on navy, logo top-right
vocabulary: [Acme, AcmeCloud]
accent: "#2f80ff"
captions: {font: Inter, weight: 700, color: "#ffffff", background: "#0b1f3a"}
bubble: {border: {color: "#2f80ff", width: 4}}
card: {background: "#0b1f3a", color: "#ffffff"}
logo: {file: acme-logo.png, corner: top-right, size: 0.1}   # next to the theme file
```

#### Title cards: `intro` / `outro`

Top-level keys: `intro: "Title"` or `intro: {title, subtitle, duration, fade_in, fade_out}` (and
the same for `outro`). A card is a full-frame title in the theme's `card` colours with an accent bar - and
`card.image` (a brand icon) big above the title - before/after the timeline, fading in and out,
with silence underneath (the audio stays in sync). The logo watermark shows on cards too.
The intro is **fully drawn on frame 0** by default (WhatsApp, LinkedIn and YouTube use the first
frame as the thumbnail; a fade-in made it a blank square); the outro fades in over 0.4 s.
`card.fade_in` / `card.fade_out` (seconds, 0 = none) change both cards; a card's own keys win.
`card.title_case: true` (a brand's house style, set in its theme) writes card titles in Title Case:
only all-lowercase words change, so names and codes (FieldPal, ACME, AC-42) stay as written, and
small words (and, of, the, ...) stay lowercase except first and last.
A **theme** may carry `intro:` / `outro:` too (a brand's standard closing card): an edit gets them
unless it sets its own, and `outro: false` / `intro: false` in edit.yaml turns them off.

#### Callouts: info boxes and section headers

```yaml
style:
  callout: {look: draw}                     # the default look for this edit (or a theme's)
callouts:                                   # source seconds, like scenes; each up for `duration`
  - {at: 12.8, title: "Acme Cloud · Reports", text: "What's new in this release:\nexports and scheduled reports"}
  - {at: 32.6, look: pill, title: Admin}
  - {at: 102.9, look: bar, title: Settings, text: "Everything in one place", to: up}
  - {at: 236.6, look: section, sparkle: true, in: 1.2, duration: 3.8, title: The live site}
```
Small animated boxes over the running video and audio, not full-frame cards: what the screen
can't say (a role, what a feature is, what's next). Few of them - one every 30-45 s at most.
- **Looks**: `draw` - the border traces itself round the box (clockwise from the top-left), then
  the fill fades in and the text fades in with a small rise; `bar` - an accent bar grows down the
  left edge and the text slides out from behind it; `pill` - one short label, faded in;
  `section` - a section header: a full-frame panel in the card colours wipes across, a big title
  and an accent bar appear on it, then it fades back to the video, which kept running (unlike
  `intro`/`outro`, which stop it). Good over a tab switch or a load.
- Titles follow `card.title_case` (a brand's house style), like the cards; `text` stays as written.
- **Travel**: `from` (`auto` = from the box's own side, `left|right|up|down|none`), `to` (out,
  `none` = just fade), `distance` (fraction of the shorter side). A section wipes in from `from`.
- **Text**: `title`, plus `text` with up to 3 lines (`\n`); a pill is a title only.
- **Timing**: `duration` (total, in and out included, default 4.5 s), `in` (0.7), `out` (0.35).
  It appears when the source reaches `at` and then runs on the output clock: a cut inside it
  doesn't end it early. One whose start was cut away still shows if the source is inside it.
- **Style** (`style.callout`, a theme's or the edit's, and any key per callout): `look`,
  `corner` (`top-left` .. `bottom-right`, `top`, `bottom`; bottom ones sit above the captions),
  `margin`, `size` / `text_size` / `padding` (fractions of the shorter side), `font` (null =
  the captions'), `weight` / `text_weight`, `color`, `background`, `opacity`, `border` (null =
  the accent), `border_width`, `radius` (px at 1080p), `sparkle`.
- **`pause: <s>`** (off by default): the recording stands still for that long at `at` - one
  held picture, silence under it - while the callout comes in, then carries on from the same
  moment. **Only where the speaker already left a long enough break** (a beat between topics,
  roughly as long as the pause): there the section gets its full wipe and nobody notices.
  Spliced into flowing speech it sounds like a glitch even in a gap between sentences - tried
  on a real demo and rejected. Find the break on the audio level, not the transcript (its word
  times can be ~0.3 s out); never mid-word. Without a break, start the section `in` before its
  scene change instead, so the wipe covers the change while the video runs.
- **`sparkle: true`** (sections only): the theme's sweep sprite gallops across leading the wipe,
  shedding its sparkle trail. Like the sparkle/sweep transitions, only with a theme that has a
  `sparkle:` section - anywhere else `check` refuses it.
- Two boxes up at once in the same corner (or two sections): `check` warns.
