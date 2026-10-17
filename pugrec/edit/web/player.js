// pugrec preview player: draws a resolved plan's layers on a canvas.
// Same layers as `pugrec build` paints (compose.py) - this file must stay dumb:
// no framing decisions here, only drawing what the plan says.
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("c");
const ctx = canvas.getContext("2d");
const state = { outputs: [], output: null, plan: null, t: 0, playing: false, last: 0, version: -1 };
const media = {};   // url -> <video>/<audio>

const hidden = document.createElement("div");
hidden.style.cssText = "position:absolute;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none";
document.body.appendChild(hidden);

function el(tag, url) {
  if (media[url]) return media[url];
  const m = document.createElement(tag);
  m.preload = "auto"; m.muted = tag === "video"; m.playsInline = true;
  m.src = url;
  // In the DOM, and one seek after metadata: a detached, never-seeked <video> may never decode a frame.
  m.addEventListener("loadedmetadata", () => { m.currentTime = Math.max(m.currentTime, 0.001); }, { once: true });
  hidden.appendChild(m);
  media[url] = m;
  return m;
}

function frameAt(t) {
  const p = state.plan;
  if (!p || !p.frames.length) return null;
  const i = Math.min(p.frames.length - 1, Math.max(0, Math.floor(t * p.output.fps + 1e-6)));
  return p.frames[i];
}

// ---- audio: no clicks at cuts ----------------------------------------------------
// A jump of the <audio> element mid-waveform clicks (the build fades 10 ms at every splice).
// So the audio goes through a gain node: down over ~12 ms, jump, back up once it has seeked.
// The AudioContext needs a user gesture, so it's made on Play; before that, plain jumps.
let actx = null;
const RAMP = 0.012;
function audioOn() {
  try {
    if (!actx) actx = new AudioContext();
    if (actx.state === "suspended") actx.resume();
  } catch (e) { actx = null; }
}
function gainFor(m) {
  if (!m._gain) {
    const src = actx.createMediaElementSource(m);
    m._gain = actx.createGain();
    src.connect(m._gain).connect(actx.destination);
  }
  return m._gain;
}
function jumpAudio(m, t) {
  if (!actx || m.tagName !== "AUDIO") { m.currentTime = t; return; }
  if (m._jumping) return;
  m._jumping = true;
  const g = gainFor(m).gain, now = actx.currentTime;
  g.cancelScheduledValues(now);
  g.setValueAtTime(g.value, now);
  g.linearRampToValueAtTime(0, now + RAMP);
  setTimeout(() => {
    m.addEventListener("seeked", () => {
      const n = actx.currentTime;
      g.cancelScheduledValues(n);
      g.setValueAtTime(0, n);
      g.linearRampToValueAtTime(1, n + RAMP);
      m._jumping = false;
    }, { once: true });
    m.currentTime = t + RAMP;   // where the plan will be by the time the fade-down is done
  }, RAMP * 1000 + 2);
}

// ---- syncing media to the plan's source times --------------------------------
function sync(frame) {
  const p = state.plan;
  const want = new Map();  // element -> file time
  for (const l of frame.layers) {
    if (l.type !== "video") continue;
    const st = p.streams[l.src];
    if (!want.has(st.url)) want.set(st.url, Math.max(0, l.t - st.offset));
  }
  if (!frame.hold) for (const a of p.audio) if (a.source === frame.src) want.set(a.url, frame.st);  // a hold is silent
  for (const [url, m] of Object.entries(media)) {
    if (!want.has(url)) { if (!m.paused) m.pause(); continue; }
    const t = want.get(url);
    const drift = Math.abs(m.currentTime - t);
    if (state.playing && !frame.hold) {   // a hold: the picture stands still, as when paused
      if (m._jumping) continue;              // a click-free jump is under way
      if (drift > 0.25) jumpAudio(m, t);     // cut, splice or drift: jump (audio: faded)
      if (m.paused) m.play().catch(() => {});
    } else {
      if (!m.paused) m.pause();
      if (drift > 0.02) m.currentTime = t;   // scrubbing / stepping
    }
  }
}

// ---- drawing (mirror of compose.py) ------------------------------------------
function shapePath(x, y, w, h, shape, r) {
  ctx.beginPath();
  if (shape === "circle") ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2);
  else if (shape === "round" && r > 0) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}

function drawVideo(l) {
  const st = state.plan.streams[l.src];
  const v = el("video", st.url);
  if (v.readyState < 2) return;
  const kx = st.proxy_size[0] / st.size[0], ky = st.proxy_size[1] / st.size[1];
  const [sx, sy, sw, sh] = l.rect, [dx, dy, dw, dh] = l.dst;
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  shapePath(dx, dy, dw, dh, l.shape, l.radius || 0);
  ctx.clip();
  const f = [];
  if (l.blur) f.push(`blur(${l.blur}px)`);
  if (l.dim) f.push(`brightness(${1 - l.dim})`);
  ctx.filter = f.length ? f.join(" ") : "none";
  // A blurred layer is drawn a little larger so the blur doesn't fade at its edges.
  const pad = l.blur ? l.blur * 2 : 0;
  ctx.drawImage(v, sx * kx, sy * ky, sw * kx, sh * ky, dx - pad, dy - pad, dw + 2 * pad, dh + 2 * pad);
  ctx.restore();
  if (l.border) {
    ctx.save();
    ctx.globalAlpha = l.opacity ?? 1;
    const [bw, color] = l.border;
    shapePath(dx + bw / 2, dy + bw / 2, dw - bw, dh - bw, l.shape, Math.max(0, (l.radius || 0) - bw / 2));
    ctx.lineWidth = bw; ctx.strokeStyle = color; ctx.stroke();
    ctx.restore();
  }
}

function drawFill(l) {
  const [x, y, w, h] = l.dst;
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  shapePath(x, y, w, h, l.shape, l.radius || 0);
  ctx.fillStyle = l.color; ctx.fill();
  ctx.restore();
}

function drawGradient(l) {
  // CSS-style angle (0 = towards the top, 90 = right); the line runs through the centre and
  // just reaches the corners - the same maths as compose.gradient_t.
  const [x, y, w, h] = l.dst;
  const a = ((l.angle ?? 180) * Math.PI) / 180, dx = Math.sin(a), dy = -Math.cos(a);
  const half = (Math.abs(w * dx) + Math.abs(h * dy)) / 2 || 1;
  const cx = x + w / 2, cy = y + h / 2;
  const g = ctx.createLinearGradient(cx - dx * half, cy - dy * half, cx + dx * half, cy + dy * half);
  l.colors.forEach((c, i) => g.addColorStop(l.colors.length > 1 ? i / (l.colors.length - 1) : 0, c));
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  shapePath(x, y, w, h, l.shape, l.radius || 0);
  ctx.fillStyle = g; ctx.fill();
  ctx.restore();
}

function drawShadow(l) {
  // Only the shadow: the shape is filled far off-canvas and its shadow offset back into place
  // (shadowBlur = 2 sigma, like compose.draw_shadow's Gaussian).
  const [x, y, w, h] = l.dst, far = 100000;
  ctx.save();
  ctx.shadowColor = `rgba(0, 0, 0, ${l.opacity ?? 0.3})`;
  ctx.shadowBlur = l.blur ?? 20;
  ctx.shadowOffsetX = far; ctx.shadowOffsetY = l.offset || 0;
  shapePath(x - far, y, w, h, "round", l.radius || 0);
  ctx.fillStyle = "#000"; ctx.fill();
  ctx.restore();
}

function drawRing(l) {
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  ctx.beginPath(); ctx.arc(l.x, l.y, l.r, 0, Math.PI * 2);
  ctx.lineWidth = l.width; ctx.strokeStyle = l.color; ctx.stroke();
  ctx.restore();
}

function drawText(l) {
  // Same geometry as compose.text_image: pill height size*1.5, `pad` each side.
  ctx.save();
  ctx.font = `${l.weight || 600} ${l.size}px "${l.font || "IBM Plex Sans"}", sans-serif`;
  ctx.textBaseline = "middle";
  const space = ctx.measureText(" ").width;
  const widths = l.words.map((w) => ctx.measureText(w.w).width);
  const tw = widths.reduce((a, b) => a + b, 0) + space * (widths.length - 1);
  const W = tw + 2 * l.pad, H = l.size * 1.5;
  const x0 = l.align === "left" ? l.x : l.x - W / 2, y0 = l.y - H / 2;
  ctx.globalAlpha = (l.opacity ?? 1) * l.bg_opacity;
  ctx.beginPath(); ctx.roundRect(x0, y0, W, H, l.radius); ctx.fillStyle = l.bg; ctx.fill();
  ctx.globalAlpha = l.opacity ?? 1;
  let x = x0 + l.pad;
  l.words.forEach((w, i) => {
    ctx.fillStyle = w.hi ? l.hi : l.color;
    ctx.fillText(w.w, x, l.y);
    x += widths[i] + space;
  });
  ctx.restore();
}

const images = {};   // url -> <img>
function drawImage(l) {
  const info = state.plan.images && state.plan.images[l.img];
  if (!info) return;
  let im = images[info.url];
  if (!im) { im = images[info.url] = new Image(); im.src = info.url; }
  if (!im.complete || !im.naturalWidth) return;
  const [x, y, w, h] = l.dst;
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  ctx.imageSmoothingEnabled = !l.pixelated;   // pixel art: nearest-neighbour, like the build
  if (l.shape && l.shape !== "rect" || l.radius) { shapePath(x, y, w, h, l.shape, l.radius || 0); ctx.clip(); }
  const [sx, sy, sw, sh] = l.rect || [0, 0, im.naturalWidth, im.naturalHeight];   // rect: a cover crop
  if (l.flip) { ctx.translate(x + w, y); ctx.scale(-1, 1); ctx.drawImage(im, sx, sy, sw, sh, 0, 0, w, h); }
  else ctx.drawImage(im, sx, sy, sw, sh, x, y, w, h);
  ctx.restore();
}

function drawStar(l) {
  // A 4-point sparkle: the same 8 corners as compose.star_points.
  if (l.r < 0.5) return;
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  ctx.beginPath();
  for (let i = 0; i < 8; i++) {
    const a = l.rot + (i * Math.PI) / 4, r = i % 2 === 0 ? l.r : l.r * 0.28;
    const px = l.x + r * Math.sin(a), py = l.y - r * Math.cos(a);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath();
  ctx.fillStyle = l.color; ctx.fill();
  ctx.restore();
}

function drawStroke(l) {
  // A callout's border, part drawn: the same polyline as compose.draw_stroke.
  if (l.points.length < 2) return;
  ctx.save();
  ctx.globalAlpha = l.opacity ?? 1;
  ctx.beginPath();
  l.points.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
  if (l.closed) ctx.closePath();
  ctx.lineWidth = l.width; ctx.lineJoin = "round"; ctx.lineCap = "round";
  ctx.strokeStyle = l.color; ctx.stroke();
  ctx.restore();
}

function draw(frame) {
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (const l of frame.layers) {
    if (l.clip) {   // e.g. the next scene during a sweep, revealed left of the sprite
      ctx.save();
      ctx.beginPath(); ctx.rect(...l.clip); ctx.clip();
    }
    if (l.type === "video") drawVideo(l);
    else if (l.type === "fill") drawFill(l);
    else if (l.type === "gradient") drawGradient(l);
    else if (l.type === "shadow") drawShadow(l);
    else if (l.type === "ring") drawRing(l);
    else if (l.type === "text") drawText(l);
    else if (l.type === "image") drawImage(l);
    else if (l.type === "star") drawStar(l);
    else if (l.type === "stroke") drawStroke(l);
    if (l.clip) ctx.restore();
  }
}

// ---- loop ----------------------------------------------------------------------
function tick(ts) {
  if (state.plan) {
    if (state.playing) {
      state.t += (ts - state.last) / 1000;
      if (state.t >= state.plan.duration) { state.t = state.plan.duration; setPlaying(false); }
    }
    const f = frameAt(state.t);
    if (f) { sync(f); draw(f); ui(f); }
  }
  state.last = ts;
  requestAnimationFrame(tick);
}

function setPlaying(on) {
  state.playing = on && !!state.plan;
  if (state.playing) audioOn();   // Play is a user gesture: the only time an AudioContext may start
  if (state.playing && state.t >= state.plan.duration - 0.05) state.t = 0;
  $("play").textContent = state.playing ? "❚❚" : "▶";
}

function seek(t) { state.t = Math.min(Math.max(0, t), state.plan ? state.plan.duration : 0); }

// ---- UI --------------------------------------------------------------------------
const fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`;
let lastScene = -1;

function ui(frame) {
  const p = state.plan;
  $("time").textContent = `${fmt(state.t)} / ${fmt(p.duration)}`;
  $("head").style.left = `${(state.t / p.duration) * 100}%`;
  if (frame.scene !== lastScene) {
    lastScene = frame.scene;
    for (const n of document.querySelectorAll("[data-scene]")) n.classList.toggle("on", +n.dataset.scene === frame.scene);
  }
}

function fitCanvas() {
  // Scale the canvas (drawn at output resolution) to fit its area - CSS max-height
  // doesn't resolve inside the grid, so a 1080x1920 output overflowed the page.
  const box = canvas.parentElement.getBoundingClientRect();
  const s = Math.min((box.width - 32) / canvas.width, (box.height - 32) / canvas.height, 1);
  canvas.style.width = `${Math.floor(canvas.width * s)}px`;
  canvas.style.height = `${Math.floor(canvas.height * s)}px`;
}
window.addEventListener("resize", fitCanvas);

function buildUI() {
  const p = state.plan;
  canvas.width = p.output.size[0]; canvas.height = p.output.size[1];
  fitCanvas();
  $("file").textContent = `~/${p.edit}`;
  const tabs = $("tabs");
  tabs.innerHTML = "";
  for (const o of state.outputs) {
    const b = document.createElement("button");
    b.textContent = o; b.className = o === state.output ? "on" : "";
    b.onclick = () => loadPlan(o);
    tabs.appendChild(b);
  }
  // Scene list + bar segments (by output time; scenes removed by cuts are greyed)
  const list = $("scenes"), bar = $("bar");
  list.innerHTML = "";
  for (const n of bar.querySelectorAll(".seg, .cut")) n.remove();
  const shown = p.scenes.filter((s) => s.out !== null).sort((a, b) => a.out - b.out);
  for (const s of p.scenes) {
    const row = document.createElement("div");
    row.className = "scene" + (s.out === null ? " cut" : "");
    row.dataset.scene = s.index;
    row.innerHTML = `<span class="i">${s.index}</span><span></span><span class="t">${s.out === null ? "cut" : fmt(s.out)}</span>`;
    row.children[1].textContent = s.label;
    if (s.out !== null) row.onclick = () => seek(s.out);
    list.appendChild(row);
  }
  shown.forEach((s, k) => {
    const end = k + 1 < shown.length ? shown[k + 1].out : p.duration;
    const seg = document.createElement("div");
    seg.className = "seg"; seg.dataset.scene = s.index;
    seg.style.left = `${(s.out / p.duration) * 100}%`;
    seg.style.width = `${((end - s.out) / p.duration) * 100}%`;
    seg.textContent = s.label;
    bar.insertBefore(seg, $("head"));
  });
  for (const sg of p.segments.slice(1)) {
    const c = document.createElement("div");
    c.className = "cut"; c.title = "cut";
    c.style.left = `${(sg.out / p.duration) * 100}%`;
    bar.insertBefore(c, $("head"));
  }
  $("warnings").innerHTML = "";
  for (const w of p.warnings) {
    const d = document.createElement("div");
    d.className = "warn"; d.textContent = `⚠ ${w}`;
    $("warnings").appendChild(d);
  }
  for (const st of Object.values(p.streams)) el("video", st.url);
  for (const a of p.audio) el("audio", a.url);
  lastScene = -1;
}

async function loadPlan(name) {
  const r = await fetch(`/plan/${name}.json`, { cache: "no-store" });
  if (!r.ok) return;
  state.plan = await r.json();
  state.output = name;
  seek(state.t);
  buildUI();
}

async function refresh(info) {
  const err = $("error"), status = $("status");
  if (info.error) {
    err.style.display = "block"; err.textContent = info.error;
    status.textContent = "edit.yaml has an error"; status.className = "err";
    return;
  }
  err.style.display = "none";
  const s = await (await fetch("/status", { cache: "no-store" })).json();
  state.outputs = s.outputs;
  await loadPlan(state.outputs.includes(state.output) ? state.output : state.outputs[0]);
  status.className = "";
  status.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ---- input -----------------------------------------------------------------------
$("play").onclick = () => setPlaying(!state.playing);
$("bar").onclick = (e) => {
  const r = $("bar").getBoundingClientRect();
  if (state.plan) seek(((e.clientX - r.left) / r.width) * state.plan.duration);
};
document.addEventListener("keydown", (e) => {
  if (!state.plan) return;
  const step = e.shiftKey ? 5 : 1;
  if (e.code === "Space") { e.preventDefault(); setPlaying(!state.playing); }
  else if (e.key === "ArrowLeft") seek(state.t - step);
  else if (e.key === "ArrowRight") seek(state.t + step);
  else if (e.key === "[" || e.key === "]") {
    const outs = state.plan.scenes.filter((s) => s.out !== null).map((s) => s.out).sort((a, b) => a - b);
    const cur = outs.filter((o) => o <= state.t + 0.01).pop() ?? 0;
    const i = outs.indexOf(cur);
    seek(e.key === "]" ? (outs[i + 1] ?? cur) : (state.t - cur > 0.5 ? cur : (outs[i - 1] ?? 0)));
  }
});

// ?debug: log every media element's state every 2 s (headless checks, sync problems).
if (location.search.includes("debug")) {
  const pre = document.createElement("pre");
  pre.id = "debug";
  pre.style.cssText = "position:fixed;left:8px;top:56px;margin:0;font:11px var(--mono);color:#9a958f;z-index:9";
  document.body.appendChild(pre);
  setInterval(() => {
    const lines = Object.entries(media).map(([url, m]) =>
      `media ${url} ready=${m.readyState} net=${m.networkState} t=${m.currentTime.toFixed(2)} paused=${m.paused} ` +
      `err=${m.error ? m.error.code + ":" + m.error.message : "-"} size=${m.videoWidth || 0}x${m.videoHeight || 0}`);
    pre.textContent = lines.join("\n");
    console.log(pre.textContent);
  }, 1000);
}

// #t=12.5 opens at that output time (shareable; handy for checking a scene).
// #o=vertical picks the output.
const hashT = parseFloat((location.hash.match(/t=([\d.]+)/) || [])[1]);
if (!Number.isNaN(hashT)) state.t = hashT;
state.output = (location.hash.match(/o=(\w+)/) || [])[1] || null;

new EventSource("/events").onmessage = (e) => {
  const info = JSON.parse(e.data);
  if (info.version !== state.version) { state.version = info.version; refresh(info); }
};
requestAnimationFrame((ts) => { state.last = ts; tick(ts); });
