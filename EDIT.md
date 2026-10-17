# Editing: the scene script (`edit.yaml`)

An edit is a YAML file next to a recording that says, **scene by scene, what the viewer sees**.
pugrec resolves it against the recording's timeline (windows, cursor, clicks, faces, transcript)
into exact frames, previews it live in the browser, and builds MP4s - 16:9 and 9:16 from one script.

```bash
pugrec transcript SESSION          # words with timestamps (GPU) - enables captions and smart cuts
pugrec draft SESSION               # a starting edit.yaml from what happened (or write one by hand)
pugrec check SESSION               # validate; list scenes and their output times per output
pugrec preview SESSION             # browser preview, updates every time edit.yaml is saved
pugrec build SESSION               # build-main.mp4, build-vertical.mp4 (--output, --from/--to, --jobs)
pugrec build SESSION --whatsapp    # plus build-<output>-whatsapp.mp4: plays inline in WhatsApp (< 16 MB)
```

**Times are source seconds** (the recording's clock, as in `session.jsonl` / `transcript.jsonl`),
not output seconds. Cutting a pause never shifts the scenes after it. The recording keeps its
warm-up (camera starting, password prompt, the 3-2-1 countdown), but **edits begin at the
`start` marker** after the countdown: by default, and when a `timeline` segment has no `from`.
To use the warm-up anyway, give a segment an explicit `from: 0`.

## A complete example

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

## Top level

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

## Scenes

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

### Shots

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

### Fit: when the shape doesn't match the output

Every shot has a target rectangle; the output (or split pane) has its own aspect.
- `cover`: show more around the target until it fills the frame; if the screen runs out, crop.
- `contain`: show exactly the target, centred, with a **blurred, dimmed copy** of the same view
  behind it (or a colour: `background: "#16171a"`).
- `auto` (default): `cover` unless that would cut off more than 15% of the target, then `contain`.
  On a 5120x2160 screen, a 16:9 output of the whole screen is `contain`; on 9:16, most windows are.
- `inset`: the picture as a **rounded card with room around it, on a backdrop** (next section).
  Only when a scene or pane asks for it; `auto` never picks it.

### Inset: a card on a backdrop

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

### Push: a slow push-in while a shot holds

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

### Punch: a quick zoom in on an important point

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

### Spotlight: one window sharp, the rest of the screen blurred

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

### Transitions

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

## Style (defaults)

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

### Themes: a customer's or brand's look

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
# ~/.config/pugrec/themes/acme.yaml
description: Acme Corp - blue on navy, logo top-right
vocabulary: [Acme, AcmeCloud]
accent: "#2f80ff"
captions: {font: Inter, weight: 700, color: "#ffffff", background: "#0b1f3a"}
bubble: {border: {color: "#2f80ff", width: 4}}
card: {background: "#0b1f3a", color: "#ffffff"}
logo: {file: acme-logo.png, corner: top-right, size: 0.1}   # next to the theme file
```

### Title cards: `intro` / `outro`

Top-level keys: `intro: "Title"` or `intro: {title, subtitle, duration, fade_in, fade_out}` (and
the same for `outro`). A card is a full-frame title in the theme's `card` colours with an accent bar - and
`card.image` (a brand icon) big above the title - before/after the timeline, fading in and out,
with silence underneath (the audio stays in sync). The logo watermark shows on cards too.
The intro is **fully drawn on frame 0** by default (WhatsApp, LinkedIn and YouTube use the first
frame as the thumbnail; a fade-in made it a blank square); the outro fades in over 0.4 s.
`card.fade_in` / `card.fade_out` (seconds, 0 = none) change both cards; a card's own keys win.
`card.title_case: true` (a brand's house style, set in its theme) writes card titles in Title Case:
only all-lowercase words change, so names and codes (FieldPal, COUPA, KAL-132) stay as written, and
small words (and, of, the, ...) stay lowercase except first and last.
A **theme** may carry `intro:` / `outro:` too (a brand's standard closing card): an edit gets them
unless it sets its own, and `outro: false` / `intro: false` in edit.yaml turns them off.

### Callouts: info boxes and section headers

```yaml
style:
  callout: {look: draw}                     # the default look for this edit (or a theme's)
callouts:                                   # source seconds, like scenes; each up for `duration`
  - {at: 12.8, title: "KAL-134 · Summaries", text: "What's going on, by role:\non home and in each project"}
  - {at: 32.6, look: pill, title: Viewer}
  - {at: 102.9, look: bar, title: For you, text: "Your tickets", to: up}
  - {at: 236.6, look: section, sparkle: true, in: 1.2, duration: 3.8, title: The real app}
```
Small animated boxes over the running video and audio, not full-frame cards: what the screen
can't say (a role, what a feature is, what's next). Few of them - one every 30-45 s at most.
- **Looks**: `draw` - the border traces itself round the box (clockwise from the top-left), then
  the fill fades in and the text fades in with a small rise; `bar` - an accent bar grows down the
  left edge and the text slides out from behind it; `pill` - one short label, faded in;
  `section` - a section header: a full-frame panel in the card colours wipes across, a big title
  and an accent bar appear on it, then it fades back to the video, which kept running (unlike
  `intro`/`outro`, which stop it). Good over a tab switch or a load. Its title follows
  `card.title_case`, like the cards.
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

## Writing an edit from a recording (for Claude, and anyone else)

1. Read `transcript.jsonl` (what was said, with word times) and the draft's window list / `session.jsonl`
   (`window_focus`, `button`, `window_open` records) to see what was on screen when.
2. Start a scene where the story changes: a new topic in the speech, a new window, a burst of clicks.
   Prefer `window ... follow: true` for "look at this app", `cursor` for "click here", `camera` for
   intros, outros and talking without the screen, `split` when talking *about* something on screen.
3. Use `cuts: {pauses: 1.2, fillers: true}` unless the pauses matter; add `ranges` for false starts.
4. Run `pugrec check` until it has no warnings you care about, then `pugrec preview` and look.
5. Give the vertical output its own shots where a wide view doesn't work (`vertical: {shot: ...}`):
   tall outputs want tight shots - a region, a cursor zoom, the camera.
