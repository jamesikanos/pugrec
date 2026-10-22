// pugrec control: a Stream Deck-style panel for `bin/pugrec` on macOS.
// The main process runs pugrec (record, audio, transcript, draft, preview, build) and the
// capture helper (screens, mics, cameras); the page only draws keys and sends presses.
// It keeps itself out of recordings (record --exclude-pid): it's on screen to be clicked.
const { app, BrowserWindow, ipcMain, shell, screen: displays } = require("electron");
const { spawn, execFile } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO = path.resolve(__dirname, "..");
const PUGREC = path.join(REPO, "bin", "pugrec");
const CAPTURE = path.join(REPO, "macos", ".build", "release", "pugrec-capture");
const SESSIONS = path.join(os.homedir(), "Movies", "PugRec");
// Started from Finder or the Dock, PATH lacks Homebrew (ffmpeg, python3, whisper-cli).
const ENV = { ...process.env, PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH || "/usr/bin:/bin"}` };

let win = null;
let recorder = null;         // the running `pugrec record`
let preview = null;          // the running `pugrec preview`
const tasks = new Map();     // key id -> child process

// ------------------------------------------------------------------ settings --
const SETTINGS = path.join(app.getPath("userData"), "settings.json");
const DEFAULTS = { screen: null, micOn: true, mic: null, cameraOn: true, camera: null, keys: false,
                   countdown: 3, vocabulary: "", session: null, onTop: false };
let settings = { ...DEFAULTS };
try { settings = { ...DEFAULTS, ...JSON.parse(fs.readFileSync(SETTINGS, "utf8")) }; } catch { /* first run */ }

function save(patch) {
  settings = { ...settings, ...patch };
  try { fs.writeFileSync(SETTINGS, JSON.stringify(settings, null, 2)); } catch { /* read-only: keep in memory */ }
  if ("onTop" in patch && win) win.setAlwaysOnTop(!!settings.onTop, "floating");
  return settings;
}

function send(channel, data) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, data);
}

// ------------------------------------------------------------------ devices --
function captureJSON(cmd) {
  return new Promise((resolve) => {
    execFile(CAPTURE, [cmd], { env: ENV, timeout: 10000 }, (err, out) => {
      if (err) return resolve(null);
      try { resolve(JSON.parse(out)); } catch { resolve(null); }
    });
  });
}

async function devices() {
  const [state, devs] = await Promise.all([captureJSON("state"), captureJSON("devices")]);
  if (!state || !devs) {
    return { error: fs.existsSync(CAPTURE) ? "pugrec-capture failed" : "run `bin/pugrec setup` first (builds pugrec-capture)" };
  }
  // Likely virtual devices (OBS, PRISM, Teams, Zoom) go last, marked, not hidden:
  // a name match can't tell a Teams-certified headset from Teams' own driver.
  const virtual = (d) => /obs|prism|teams|zoom|virtual/i.test(d.name);
  const order = (list) => [...list.filter((d) => !virtual(d)), ...list.filter(virtual).map((d) => ({ ...d, virtual: true }))];
  return {
    monitors: state.monitors.map((m) => ({ name: m.name, id: m.id, width: m.width, height: m.height, focused: m.focused })),
    cameras: order(devs.cameras),
    mics: order(devs.mics),
    permissions: state.permissions,
  };
}

// ----------------------------------------------------------------- sessions --
function readSession(dir) {
  const has = (f) => fs.existsSync(path.join(dir, f));
  let duration = null;
  try {
    const lines = fs.readFileSync(path.join(dir, "session.jsonl"), "utf8").trimEnd().split("\n");
    const end = JSON.parse(lines[lines.length - 1]);
    if (end.type === "session_end") duration = end.duration / 1e9;
  } catch { /* still recording, or crashed */ }
  return {
    path: dir, name: path.basename(dir), duration,
    done: {
      audio: has("mic-clean.flac"), transcript: has("transcript.jsonl"), draft: has("edit.yaml"),
      main: has("build-main.mp4"), vertical: has("build-vertical.mp4"),
      whatsapp: has("build-vertical-whatsapp.mp4") || has("build-main-whatsapp.mp4"),
    },
  };
}

function sessions() {
  let names = [];
  try {
    names = fs.readdirSync(SESSIONS).filter((n) => /^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d$/.test(n)
      && fs.existsSync(path.join(SESSIONS, n, "session.jsonl")));
  } catch { /* no sessions yet */ }
  return names.sort().reverse().slice(0, 20).map((n) => readSession(path.join(SESSIONS, n)));
}

function current() {
  const list = sessions();
  const s = list.find((x) => x.path === settings.session) || list[0] || null;
  return { session: s, sessions: list };
}

// ------------------------------------------------------------------- record --
function startRecording() {
  if (recorder) return;
  const args = ["record", "--exclude-pid", String(process.pid), "--countdown", String(settings.countdown)];
  if (settings.screen) args.push("--output", settings.screen);
  if (!settings.micOn) args.push("--no-mic");
  else if (settings.mic) args.push("--mic", settings.mic);
  if (!settings.cameraOn) args.push("--no-camera");
  else if (settings.camera) args.push("--camera", settings.camera);
  args.push(settings.keys ? "--keys" : "--no-keys");
  recorder = spawn(PUGREC, args, { env: ENV, cwd: REPO });
  send("rec", { state: "starting" });
  let buf = "";
  recorder.stderr.on("data", (d) => {
    buf += d.toString();
    const lines = buf.split(/\r?\n/);
    buf = lines.pop();
    for (const line of lines) {
      send("log", line);
      if (line.includes("● recording")) send("rec", { state: "recording", since: Date.now() });
      else if (line.includes("countdown on screen")) send("rec", { state: "countdown" });
      const saved = line.match(/pugrec: saved (.+)$/);
      if (saved) save({ session: saved[1].trim() });
    }
  });
  recorder.on("exit", (code) => {
    recorder = null;
    send("rec", { state: "idle", code });
    send("sessions", current());
  });
}

function stopRecording() {
  if (recorder) recorder.kill("SIGINT");  // the recorder finalises the files on SIGINT, as with Ctrl+C
}

// -------------------------------------------------------------------- tasks --
// Key id -> the pugrec command it runs on the current session.
function taskArgs(id, s) {
  switch (id) {
    case "audio": return ["audio", s.path];
    case "transcript": {
      const a = ["transcript", s.path];
      if (settings.vocabulary.trim()) a.push("--prompt", settings.vocabulary.trim());
      return a;
    }
    case "draft": return ["draft", s.path];
    case "main": return ["build", s.path, "--output", "main"];
    case "vertical": return ["build", s.path, "--output", "vertical"];
    case "whatsapp": {
      const src = ["build-vertical.mp4", "build-main.mp4"].map((f) => path.join(s.path, f)).find((f) => fs.existsSync(f));
      return src ? ["whatsapp", src] : null;
    }
    default: return null;
  }
}

// Progress from pugrec's own output: "build main:  3.0/11.0 s", "audio: 2/3 ...".
function progressOf(line) {
  let m = line.match(/build \w+:\s+([\d.]+)\/([\d.]+) s/);
  if (m) return Math.min(1, parseFloat(m[1]) / parseFloat(m[2]));
  m = line.match(/^audio: (\d)\/3/);
  if (m) return (parseInt(m[1], 10) - 1) / 3;
  return null;
}

function runTask(id) {
  if (tasks.has(id)) return;
  const { session } = current();
  if (!session) return send("log", "no session yet: record one first");
  const args = taskArgs(id, session);
  if (!args) return send("task", { id, state: "error", line: "build a video first" });
  const child = spawn(PUGREC, args, { env: ENV, cwd: REPO });
  tasks.set(id, child);
  send("task", { id, state: "running", progress: null });
  let last = "";
  let buf = "";
  const onData = (d) => {
    buf += d.toString();
    const parts = buf.split(/[\r\n]/);
    buf = parts.pop();
    for (const line of parts.map((l) => l.trim()).filter(Boolean)) {
      last = line;
      const p = progressOf(line);
      send("task", { id, state: "running", progress: p, line });
      send("log", line);
    }
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);
  child.on("exit", (code) => {
    tasks.delete(id);
    send("task", { id, state: code === 0 ? "done" : "error", line: code === 0 ? "" : last });
    send("sessions", current());
  });
}

function togglePreview() {
  if (preview) {
    preview.kill("SIGINT");
    return;
  }
  const { session } = current();
  if (!session) return;
  preview = spawn(PUGREC, ["preview", session.path], { env: ENV, cwd: REPO });
  send("task", { id: "preview", state: "running", progress: null });
  const onData = (d) => d.toString().split(/\r?\n/).filter(Boolean).forEach((l) => send("log", l));
  preview.stdout.on("data", onData);
  preview.stderr.on("data", onData);
  preview.on("exit", (code) => {
    preview = null;
    send("task", { id: "preview", state: code === 0 || code === null || code === 130 ? "idle" : "error" });
  });
}

// ---------------------------------------------------------------------- IPC --
ipcMain.handle("init", async () => ({ settings, devices: await devices(), ...current(),
                                      recording: !!recorder, running: [...tasks.keys()], preview: !!preview }));
ipcMain.handle("devices", () => devices());
ipcMain.handle("sessions", () => current());
ipcMain.handle("settings", (_e, patch) => save(patch));
ipcMain.on("record", () => (recorder ? stopRecording() : startRecording()));
ipcMain.on("task", (_e, id) => runTask(id));
ipcMain.on("preview", () => togglePreview());
ipcMain.on("open", (_e, what) => {
  const { session } = current();
  if (!session) return;
  if (what === "folder") shell.openPath(session.path);
  if (what === "edit") {
    const f = path.join(session.path, "edit.yaml");
    if (fs.existsSync(f)) shell.openPath(f);
    else send("log", "no edit.yaml yet: press Draft first");
  }
});

// ------------------------------------------------------------------- window --
function createWindow() {
  win = new BrowserWindow({
    width: 820, height: 600, minWidth: 640, minHeight: 470,
    title: "pugrec", backgroundColor: "#0c0c0e",
    titleBarStyle: "hiddenInset", alwaysOnTop: !!settings.onTop,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true },
  });
  win.loadFile(path.join(__dirname, "index.html"));
  // Nothing remote, ever: links open in the browser, navigation away is refused.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  // The page's errors, in the terminal that started the app.
  win.webContents.on("console-message", (e) => {
    if (e.level === "error" || e.level === "warning") console.error(`[page] ${e.message} (${e.sourceId}:${e.lineNumber})`);
  });
}

app.whenReady().then(() => {
  createWindow();
  // Sessions change on disk while builds run elsewhere too: a cheap poll keeps the keys right.
  setInterval(() => send("sessions", current()), 3000);
  displays.on("display-added", async () => send("devices", await devices()));
  displays.on("display-removed", async () => send("devices", await devices()));
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  stopRecording();          // a recording in progress is saved, not lost
  if (preview) preview.kill("SIGINT");
});
