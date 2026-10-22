// pugrec control: the keys. Everything real happens in main.js; this draws state and sends presses.
// Click = the key's action. Right-click or a long press on Screen / Mic / Camera / Session = choose.

const svg = (body, extra = "") => `<svg viewBox="0 0 24 24" aria-hidden="true" ${extra}>${body}</svg>`;
const ICONS = {
  rec: svg('<circle cx="12" cy="12" r="7.5" fill="currentColor" stroke="none"/>'),
  stop: svg('<rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none"/>'),
  screen: svg('<rect x="3" y="4.5" width="18" height="12" rx="1.8"/><path d="M9 20h6M12 16.5V20"/>'),
  mic: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>'),
  micOff: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M4 4l16 16"/>'),
  camera: svg('<rect x="3" y="6.5" width="12.5" height="11" rx="2"/><path d="m15.5 10.5 5-3v9l-5-3"/>'),
  cameraOff: svg('<rect x="3" y="6.5" width="12.5" height="11" rx="2"/><path d="m15.5 10.5 5-3v9l-5-3M3 3l18 18"/>'),
  keys: svg('<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 9.5h.01M9.3 9.5h.01M12.6 9.5h.01M15.9 9.5h.01M18 9.5h.01M7 14.5h10"/>'),
  session: svg('<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M3.5 9h17M8 5l2 4M13 5l2 4"/>'),
  audio: svg('<path d="M3 12h2M7 8v8M10 5v14M13 9v6M16 6.5v11M19 10v4M21 12h0"/>'),
  transcript: svg('<path d="M4 5.5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/>'),
  draft: svg('<path d="m4 20 10.5-10.5M14.5 9.5 17 7M15 3v2.5M19 5l-1.6 1.6M21 9h-2.5M12 4.5l1 1"/>'),
  preview: svg('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>'),
  main: svg('<rect x="2.5" y="6.5" width="19" height="11" rx="1.5"/><path d="m10.5 9.8 4 2.2-4 2.2z" fill="currentColor"/>'),
  vertical: svg('<rect x="7.5" y="2.5" width="9" height="19" rx="1.5"/><path d="m10.8 10 3.2 2-3.2 2z" fill="currentColor"/>'),
  whatsapp: svg('<path d="m21 3-9.5 9.5M21 3l-6 18-3.5-8.5L3 9z"/>'),
  folder: svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  edit: svg('<path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5V8h4M9 12.5h6M9 16h4"/>'),
};

const TASKS = ["audio", "transcript", "draft", "main", "vertical", "whatsapp"];
const KEYS = [
  { id: "rec", label: "Record" },
  { id: "screen", label: "Screen", pick: true },
  { id: "mic", label: "Mic", pick: true },
  { id: "camera", label: "Camera", pick: true },
  { id: "keys", label: "Keys" },
  { id: "session", label: "Session", pick: true },
  { id: "audio", label: "Clean audio" },
  { id: "transcript", label: "Transcript" },
  { id: "draft", label: "Draft edit" },
  { id: "preview", label: "Preview" },
  { id: "main", label: "Build 16:9" },
  { id: "vertical", label: "Build 9:16" },
  { id: "whatsapp", label: "WhatsApp" },
  { id: "folder", label: "Folder" },
  { id: "edit", label: "Edit script" },
];

const S = {
  settings: {}, devices: {}, session: null, sessions: [],
  rec: "idle", since: 0, tasks: {}, preview: false,
};
const el = {};     // page elements (strip, picker, settings)
const keyEl = {};  // key id -> its button (separate: "session" is both a key and a strip label)

// ------------------------------------------------------------------ helpers --
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const recording = () => S.rec !== "idle";

function sessionLabel(s) {
  if (!s) return "none";
  const [d, t] = s.name.split("_");
  const date = new Date(`${d}T${t.replace(/-/g, ":")}`);
  return date.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function screenOf() {
  const mons = S.devices.monitors || [];
  return mons.find((m) => m.name === S.settings.screen) || null;
}

function shortName(n) {
  return n.replace(/^Built-in Retina Display$/, "Built-in").replace(/^MacBook Pro /, "MacBook ");
}

// -------------------------------------------------------------------- state --
function keyState(id) {
  const st = S.settings;
  const t = S.tasks[id] || {};
  const noSession = !S.session;
  const out = { icon: ICONS[id], label: null, sub: "", cls: [], disabled: false };
  switch (id) {
    case "rec":
      out.cls.push("rec");
      if (S.rec === "recording") {
        out.icon = ICONS.stop; out.label = fmtTime((Date.now() - S.since) / 1000); out.sub = "Click to stop";
        out.cls.push("live");
      } else if (S.rec === "starting" || S.rec === "countdown") {
        out.icon = ICONS.stop; out.sub = S.rec === "countdown" ? "Counting down" : "Starting…"; out.cls.push("wait");
      } else {
        out.sub = screenOf() ? shortName(screenOf().name) : "Screen with the cursor";
      }
      break;
    case "screen": {
      const m = screenOf();
      out.sub = m ? `${shortName(m.name)} · ${m.width}×${m.height}` : "With the cursor";
      out.disabled = recording();
      break;
    }
    case "mic":
      out.icon = st.micOn ? ICONS.mic : ICONS.micOff;
      out.cls.push(st.micOn ? "on" : "off");
      out.sub = st.micOn ? shortName(st.mic || "Default") : "Off";
      out.disabled = recording();
      break;
    case "camera":
      out.icon = st.cameraOn ? ICONS.camera : ICONS.cameraOff;
      out.cls.push(st.cameraOn ? "on" : "off");
      out.sub = st.cameraOn ? shortName(st.camera || "Default") : "Off";
      out.disabled = recording();
      break;
    case "keys":
      out.cls.push(st.keys ? "on" : "off");
      out.sub = st.keys ? "Recording keys" : "Clicks only";
      out.disabled = recording();
      break;
    case "session":
      out.sub = S.session ? `${sessionLabel(S.session)}${S.session.duration ? " · " + fmtTime(S.session.duration) : ""}` : "None yet";
      out.disabled = recording() || !S.sessions.length;
      break;
    case "preview":
      out.disabled = noSession || recording();
      if (S.preview) { out.cls.push("running", "indeterminate"); out.sub = "Running · stop"; }
      break;
    case "folder":
      out.disabled = noSession;
      break;
    case "edit":
      out.disabled = noSession || !S.session.done.draft;
      out.sub = out.disabled && !noSession ? "Draft first" : "";
      break;
    default:  // pipeline tasks
      out.disabled = noSession || recording() || (id === "whatsapp" && !(S.session.done.main || S.session.done.vertical));
      if (t.state === "running") {
        out.cls.push("running");
        if (t.progress == null) { out.cls.push("indeterminate"); out.sub = "Working…"; }
        else out.sub = `${Math.round(t.progress * 100)}%`;
      } else if (t.state === "error") {
        out.cls.push("error"); out.sub = t.line ? t.line.replace(/^\w+: /, "") : "Failed";
      } else if (S.session && S.session.done[id]) {
        out.cls.push("done"); out.sub = "Done";
      } else if (id === "whatsapp" && !out.disabled) {
        out.sub = "< 16 MB copy";
      }
  }
  return out;
}

function render() {
  for (const k of KEYS) {
    const b = keyEl[k.id];
    const s = keyState(k.id);
    b.className = ["key", ...s.cls].join(" ");
    b.disabled = s.disabled;
    b.style.setProperty("--p", (S.tasks[k.id] && S.tasks[k.id].progress) || 0);
    if (b._icon !== s.icon) { b.querySelector(".icon").innerHTML = s.icon; b._icon = s.icon; }
    b.querySelector(".label").textContent = s.label || k.label;
    b.querySelector(".sub").textContent = s.sub;
    b.title = s.sub ? `${k.label}: ${s.sub}` : k.label;
    b.setAttribute("aria-label", b.title);
  }
  el.session.textContent = S.session ? S.session.name : "no session";
  const busy = Object.values(S.tasks).some((t) => t.state === "running");
  el.led.className = "led " + (S.rec === "recording" ? "live" : busy || recording() ? "busy" : "ok");
}

// ------------------------------------------------------------------ actions --
async function setSettings(patch) {
  S.settings = await window.pugrec.settings(patch);
  render();
}

function press(id) {
  const st = S.settings;
  switch (id) {
    case "rec": window.pugrec.record(); return;
    case "screen": {  // cycle: cursor's screen -> each monitor -> ...
      const names = [null, ...(S.devices.monitors || []).map((m) => m.name)];
      setSettings({ screen: names[(names.indexOf(st.screen) + 1) % names.length] });
      return;
    }
    case "mic": setSettings({ micOn: !st.micOn }); return;
    case "camera": setSettings({ cameraOn: !st.cameraOn }); return;
    case "keys": setSettings({ keys: !st.keys }); return;
    case "session": openPicker("session"); return;
    case "preview": window.pugrec.preview(); return;
    case "folder": window.pugrec.open("folder"); return;
    case "edit": window.pugrec.open("edit"); return;
    default:
      if (TASKS.includes(id)) {
        S.tasks[id] = { state: "running", progress: null };
        render();
        window.pugrec.task(id);
      }
  }
}

// ------------------------------------------------------------------- picker --
function openPicker(id) {
  const b = keyEl[id];
  if (b.disabled) return;
  const st = S.settings;
  let title, items;
  if (id === "screen") {
    title = "Screen to record";
    items = [{ v: null, t: "The one with the cursor", sel: !st.screen },
             ...(S.devices.monitors || []).map((m) => ({ v: m.name, t: m.name, s: `${m.width}×${m.height}`, sel: m.name === st.screen }))];
  } else if (id === "mic") {
    title = "Microphone";
    items = [{ v: "__off", t: "Off", sel: !st.micOn }, { v: null, t: "System default", sel: st.micOn && !st.mic },
             ...(S.devices.mics || []).map((d) => ({ v: d.name, t: d.name, s: d.virtual ? "virtual?" : "", sel: st.micOn && d.name === st.mic }))];
  } else if (id === "camera") {
    title = "Camera";
    items = [{ v: "__off", t: "Off", sel: !st.cameraOn }, { v: null, t: "System default", sel: st.cameraOn && !st.camera },
             ...(S.devices.cameras || []).map((d) => ({ v: d.name, t: d.name, s: d.virtual ? "virtual?" : "", sel: st.cameraOn && d.name === st.camera }))];
  } else {
    title = "Session";
    items = S.sessions.map((s) => ({ v: s.path, t: sessionLabel(s), s: s.duration ? fmtTime(s.duration) : "",
                                     sel: S.session && s.path === S.session.path }));
  }
  const p = el.picker;
  p.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = title;
  p.append(h);
  for (const it of items) {
    const o = document.createElement("button");
    o.setAttribute("role", "option");
    o.setAttribute("aria-selected", it.sel ? "true" : "false");
    const name = document.createElement("span");
    name.textContent = it.t;
    o.append(name);
    if (it.s) { const sm = document.createElement("small"); sm.textContent = it.s; o.append(sm); }
    o.addEventListener("click", () => { choose(id, it.v); closePicker(); });
    p.append(o);
  }
  const r = b.getBoundingClientRect();
  p.hidden = false;
  const left = Math.min(r.left, window.innerWidth - p.offsetWidth - 12);
  const top = r.bottom + 8 + p.offsetHeight > window.innerHeight ? Math.max(12, r.top - p.offsetHeight - 8) : r.bottom + 8;
  p.style.left = `${Math.max(12, left)}px`;
  p.style.top = `${top}px`;
  p.querySelector('[aria-selected="true"]')?.focus() || p.querySelector("button")?.focus();
}

function closePicker() { el.picker.hidden = true; }

function choose(id, v) {
  if (id === "screen") setSettings({ screen: v });
  else if (id === "mic") setSettings(v === "__off" ? { micOn: false } : { micOn: true, mic: v });
  else if (id === "camera") setSettings(v === "__off" ? { cameraOn: false } : { cameraOn: true, camera: v });
  else if (id === "session") { setSettings({ session: v }).then(refreshSessions); S.tasks = {}; }
}

async function refreshSessions(data) {
  const d = data || await window.pugrec.sessions();
  S.session = d.session;
  S.sessions = d.sessions;
  render();
}

// --------------------------------------------------------------------- build --
function build() {
  const grid = document.getElementById("keys");
  for (const k of KEYS) {
    const b = document.createElement("button");
    b.className = "key";
    b.innerHTML = '<span class="icon"></span><span class="label"></span><span class="sub"></span>';
    b.addEventListener("click", () => { if (!b._longPressed) press(k.id); b._longPressed = false; });
    if (k.pick) {
      b.addEventListener("contextmenu", (e) => { e.preventDefault(); openPicker(k.id); });
      let timer = null;
      b.addEventListener("pointerdown", () => {
        timer = setTimeout(() => { b._longPressed = true; openPicker(k.id); }, 500);
      });
      for (const ev of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(ev, () => clearTimeout(timer));
    }
    keyEl[k.id] = b;
    grid.append(b);
  }
  for (const id of ["session", "log", "led", "picker", "gear", "sheet", "settings", "perm"]) el[id] = document.getElementById(id);

  document.addEventListener("pointerdown", (e) => {
    if (!el.picker.hidden && !el.picker.contains(e.target)) closePicker();
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePicker(); el.sheet.hidden = true; }
  });

  el.gear.addEventListener("click", () => {
    const f = el.settings;
    f.countdown.value = String(S.settings.countdown);
    f.vocabulary.value = S.settings.vocabulary || "";
    f.onTop.checked = !!S.settings.onTop;
    const p = S.devices.permissions || {};
    el.perm.textContent = p.screen === false
      ? "Screen Recording isn't allowed for this app yet: System Settings > Privacy & Security."
      : "Permissions are asked for in this app's name (Electron) the first time you record.";
    el.sheet.hidden = false;
  });
  el.settings.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = el.settings;
    setSettings({ countdown: parseInt(f.countdown.value, 10), vocabulary: f.vocabulary.value, onTop: f.onTop.checked });
    el.sheet.hidden = true;
  });
  el.sheet.addEventListener("click", (e) => { if (e.target === el.sheet) el.sheet.hidden = true; });
}

// ---------------------------------------------------------------------- run --
async function start() {
  build();
  const d = await window.pugrec.init();
  S.settings = d.settings;
  S.devices = d.devices;
  S.session = d.session;
  S.sessions = d.sessions;
  S.preview = d.preview;
  if (d.recording) S.rec = "starting";
  for (const id of d.running) S.tasks[id] = { state: "running", progress: null };
  if (d.devices.error) el.log.textContent = d.devices.error;
  render();

  window.pugrec.on("rec", (r) => {
    S.rec = r.state;
    if (r.since) S.since = r.since;
    if (r.state === "idle") S.tasks = {};
    render();
  });
  window.pugrec.on("task", (t) => {
    if (t.id === "preview") { S.preview = t.state === "running"; render(); return; }
    S.tasks[t.id] = { ...(S.tasks[t.id] || {}), ...t, progress: t.progress ?? (S.tasks[t.id] || {}).progress ?? null };
    if (t.state === "done") delete S.tasks[t.id];  // the file on disk says it's done from now on
    render();
  });
  window.pugrec.on("log", (line) => { el.log.textContent = line; });
  window.pugrec.on("sessions", (data) => refreshSessions(data));
  window.pugrec.on("devices", (dv) => { S.devices = dv; render(); });
  setInterval(() => { if (S.rec === "recording") render(); }, 250);
}

start();
