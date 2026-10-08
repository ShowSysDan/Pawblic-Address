// Mic -> AudioWorklet (48 kHz Int16 PCM) -> WebSocket -> server -> always-on stream.
// Everything here is tuned for latency: no MediaRecorder, no browser DSP, small chunks,
// and chunks are dropped rather than queued if the link stalls.
//
// The first tap opens the mic and connects; after that the button only mutes and unmutes,
// which is instant. The mic is released when the page is hidden or Release is tapped.

const RATE = 48000;            // the server's format; the worklet resamples if it must
const CHUNK_SAMPLES = 512;     // PCM per WebSocket message: 10.7 ms
const MAX_BACKLOG_MS = 60;     // If this much is unsent, drop new chunks to stay live.
const PING_EVERY_MS = 2000;    // keepalive while connected, and the link latency readout
const READY_TIMEOUT_MS = 5000;
const HEALTH_EVERY_MS = 5000;  // How often to check the server is answering.
const HEALTH_TIMEOUT_MS = 3000;
const RTT_SAMPLES = 10;        // link latency: jitter over the last this many pings
const RTT_SLOW_MS = 100;       // round trip above this, or a lost ping: show the link as poor
const NOTE_FADE_MS = 4000;     // "Saved." fades after this long

const VERSION = document.documentElement.dataset.version;
const WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

const $ = (id) => document.getElementById(id);
const liveBtn = $("liveBtn");
const statusEl = $("status");
const releaseBtn = $("releaseBtn");
const sidetoneBox = $("sidetone");
const holdBox = $("holdToTalk");
const meterEl = $("meter");
const meterBar = meterEl.firstElementChild;

let ctx, stream, source, worklet, silent, sidetone, ws, wakeLock, pingTimer;
let connected = false; // mic open and the server said ready
let live = false;      // holding the floor
let sending = false;   // audio is going out (from the tap, before the server confirms)

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function render() {
  document.body.classList.toggle("live", live);
  if (holdBox.checked) liveBtn.textContent = !connected ? "Turn on mic" : live ? "Release to mute" : "Hold to talk";
  else liveBtn.textContent = live ? "Mute" : "Go live";
  liveBtn.setAttribute("aria-pressed", String(live));
  releaseBtn.hidden = !connected;
  meterEl.hidden = !connected;
  syncSidetone();
}

// ---- Level meter: the mic's peak, live or muted, so a talker can see it's working --

let shown = 0;  // dB shown, falls back slowly like a real meter

function showLevel(peak) {
  const db = peak > 0 ? 20 * Math.log10(peak) : -100;
  shown = Math.max(db, shown - 3);  // ~60 dB/s fall at 20 updates a second
  const pct = Math.max(0, Math.min(100, (shown + 60) / 60 * 100));
  meterBar.style.width = pct + "%";
  meterEl.dataset.level = shown > -3 ? "clip" : shown > -12 ? "hot" : "ok";
}

// Sidetone: the mic straight to this phone's output, while live, so the talker can hear
// themselves in headphones. Browser echo cancellation is off, so on a speaker it howls.
function syncSidetone() {
  if (sidetone) sidetone.gain.value = live && sidetoneBox.checked ? 1 : 0;
}

try { sidetoneBox.checked = localStorage.getItem("pa.sidetone") === "1"; } catch (_) {}
sidetoneBox.addEventListener("change", () => {
  try { localStorage.setItem("pa.sidetone", sidetoneBox.checked ? "1" : "0"); } catch (_) {}
  syncSidetone();
});

async function holdWakeLock(on) {
  try {
    if (on && !wakeLock) wakeLock = await navigator.wakeLock?.request("screen");
    else if (!on && wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch (_) {}
}

function onServerMessage(e) {
  let m;
  try { m = JSON.parse(e.data); } catch (_) { return; }
  if (m.type === "live") {
    if (!sending) return; // muted again before the server answered; our mute follows
    live = true;
    render();
    holdWakeLock(true);
    setStatus("Live, " + m.msg, "live");
  } else if (m.type === "muted") {
    setSending(false);
    live = false;
    render();
    holdWakeLock(false);
    setStatus(m.msg || readyText(), m.msg ? "error" : "");
  } else if (m.type === "refused") {
    setSending(false);
    live = false;
    render();
    setStatus(m.msg, "error");
  } else if (m.type === "error") {
    setStatus(m.msg, "error");
    disconnect(1000, true);
  }
}

function readyText() {
  return holdBox.checked ? "Mic on. Hold the button to talk." : "Muted. The mic is ready: tap Go live to talk.";
}

function setSending(on) {
  sending = on;
  worklet?.port.postMessage({ sending: on });
}

async function connect() {
  setStatus("Asking for the microphone…");
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      // Browser DSP adds delay and colours a PA feed; keep the mic raw.
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
  stream.getAudioTracks()[0].onended = () => {
    if (connected) { setStatus("The microphone was taken away (a call?). Tap to go live again.", "error"); disconnect(1000, true); }
  };

  // Must be created inside the tap handler so iOS lets it run.
  ctx = new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
  await ctx.resume();
  await ctx.audioWorklet.addModule("/static/pcm-worklet.js?v=" + VERSION);

  setStatus("Connecting…");
  ws = new WebSocket(WS_BASE + "/ws/audio");
  ws.binaryType = "arraybuffer";
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("Could not reach the server"));
  });
  ws.send(JSON.stringify({ type: "hello", sampleRate: RATE }));
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("The server didn't answer")), READY_TIMEOUT_MS);
    ws.onmessage = (e) => {
      clearTimeout(timer);
      let m = {};
      try { m = JSON.parse(e.data); } catch (_) {}
      if (m.type === "ready") resolve();
      else reject(new Error(m.msg || "The server refused the connection"));
    };
    ws.onclose = () => { clearTimeout(timer); reject(new Error("The server closed the connection")); };
  });
  ws.onmessage = onServerMessage;
  ws.onclose = () => {
    if (connected) { setStatus("Lost the connection to the server. Tap to go live again.", "error"); disconnect(1000, true); }
  };

  const backlogBytes = RATE * 2 * MAX_BACKLOG_MS / 1000;
  source = ctx.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(ctx, "pcm-sender", {
    numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
    processorOptions: { chunkSamples: CHUNK_SAMPLES },
  });
  worklet.port.onmessage = (e) => {
    if (!(e.data instanceof ArrayBuffer)) return showLevel(e.data.peak);
    if (!sending || ws?.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > backlogBytes) return; // stay live, skip this chunk
    ws.send(e.data);
  };
  // Silent path to the output keeps the worklet scheduled on every browser.
  silent = ctx.createGain();
  silent.gain.value = 0;
  source.connect(worklet).connect(silent).connect(ctx.destination);
  sidetone = ctx.createGain();
  sidetone.gain.value = 0;
  source.connect(sidetone).connect(ctx.destination);

  const ping = () => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
  };
  ping();
  pingTimer = setInterval(ping, PING_EVERY_MS);
  connected = true;
  render();
}

function talk() {
  if (!connected || sending) return;
  // Audio goes out from now; the server plays it only if the floor was free.
  setSending(true);
  ws.send(JSON.stringify({ type: "talk" }));
  setStatus("Going live…");
}

function mute() {
  if (!connected || (!sending && !live)) return;
  setSending(false);
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "mute" }));
  live = false;
  render();
  holdWakeLock(false);
  setStatus(readyText());
}

// code 1000: Release tapped (or an error); 1001: the page was hidden.
async function disconnect(code = 1000, keepStatus = false) {
  connected = live = false;
  setSending(false);
  render();
  clearInterval(pingTimer);
  try { source?.disconnect(); worklet?.disconnect(); silent?.disconnect(); sidetone?.disconnect(); } catch (_) {}
  stream?.getTracks().forEach((t) => t.stop());
  if (ws) { ws.onclose = null; if (ws.readyState <= WebSocket.OPEN) ws.close(code, "release"); }
  try { await ctx?.close(); } catch (_) {}
  await holdWakeLock(false);
  ctx = stream = source = worklet = silent = sidetone = ws = pingTimer = null;
  if (!keepStatus) setStatus("Ready");
}

// The first tap opens the mic. Browsers only allow that from a completed tap (not a
// touch going down), so in hold-to-talk mode that first tap just turns the mic on.
liveBtn.addEventListener("click", async () => {
  if (holdBox.checked && connected) return; // handled by press and release below
  if (live || sending) return mute();
  if (connected) return talk();
  liveBtn.disabled = true;
  try {
    await connect();
    if (holdBox.checked) setStatus(readyText());
    else talk();
  } catch (err) {
    const msg = err.name === "NotAllowedError"
      ? "Microphone access was blocked. Allow it in the browser and try again."
      : err.message || String(err);
    setStatus(msg, "error");
    await disconnect(1000, true);
  } finally {
    liveBtn.disabled = false;
  }
});

releaseBtn.addEventListener("click", () => disconnect(1000));

// ---- Hold to talk -----------------------------------------------------------

function press(e) {
  if (!holdBox.checked || !connected) return;
  e.preventDefault();
  liveBtn.setPointerCapture?.(e.pointerId);
  talk();
}

function letGo() {
  if (holdBox.checked) mute();
}

liveBtn.addEventListener("pointerdown", press);
liveBtn.addEventListener("pointerup", letGo);
liveBtn.addEventListener("pointercancel", letGo);
liveBtn.addEventListener("contextmenu", (e) => { if (holdBox.checked) e.preventDefault(); });

// Space bar on a computer, unless typing in a field.
const typing = () => /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName);
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || !holdBox.checked || !connected || typing()) return;
  e.preventDefault();
  if (!e.repeat) talk();
});
document.addEventListener("keyup", (e) => {
  if (e.code !== "Space" || !holdBox.checked || typing()) return;
  e.preventDefault();
  mute();
});
// Losing the page while holding counts as letting go.
window.addEventListener("blur", letGo);

try { holdBox.checked = localStorage.getItem("pa.hold") === "1"; } catch (_) {}
holdBox.addEventListener("change", () => {
  try { localStorage.setItem("pa.hold", holdBox.checked ? "1" : "0"); } catch (_) {}
  if (holdBox.checked && live) mute();
  render();
});
render();

// ---- Settings -------------------------------------------------------------

const fields = ["ip", "port", "codec", "bitrate", "max_talk_min", "syslog_host", "syslog_port"];
const saveNote = $("saveNote");
const SETTINGS_ERROR = "Could not load settings from the server";
let settingsLoaded = false;
let settingsLoading = false;

function syncCodecRow() {
  $("bitrateRow").hidden = $("codec").value !== "mp3";
}

let noteTimer;

// Success notes fade after a few seconds, so a second save visibly says "Saved." again.
// Errors stay until the next try.
function note(text, kind = "") {
  clearTimeout(noteTimer);
  saveNote.classList.remove("fade");
  saveNote.textContent = text;
  saveNote.dataset.kind = kind;
  if (kind !== "error") noteTimer = setTimeout(() => saveNote.classList.add("fade"), NOTE_FADE_MS);
}

async function loadSettings() {
  settingsLoading = true;
  try {
    const cfg = await (await fetch("/api/settings")).json();
    fields.forEach((f) => { $(f).value = cfg[f]; });
    syncCodecRow();
    settingsLoaded = true;
  } finally {
    settingsLoading = false;
  }
}

// POST JSON; resolves to the parsed reply, throws with the server's message on failure.
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || `Server error (${res.status})`);
  return data;
}

async function saveSettings() {
  const body = Object.fromEntries(fields.map((f) => [f, $(f).value]));
  const data = await postJSON("/api/settings", body);
  fields.forEach((f) => { $(f).value = data[f]; });
  syncCodecRow();
}

$("codec").addEventListener("change", syncCodecRow);

$("saveBtn").addEventListener("click", async () => {
  try {
    await saveSettings();
    note("Saved. The stream to the Core now uses these settings.");
  } catch (err) {
    note(err.message, "error");
  }
});

// Saves first, so the test goes to what's on screen.
$("testBtn").addEventListener("click", async () => {
  $("testBtn").disabled = true;
  try {
    await saveSettings();
    const data = await postJSON("/api/syslog/test");
    note(`Saved, and sent a test message to ${data.sent_to}.`);
  } catch (err) {
    note(err.message, "error");
  } finally {
    $("testBtn").disabled = false;
  }
});

// ---- Server connection indicator ------------------------------------------

const conn = $("conn");
const connText = $("connText");
const reloadBtn = $("reloadBtn");
let failures = 0;
let healthTimer;

let connState = "checking";

function setConn(state, text) {
  connState = state;
  conn.dataset.state = linkPoor && state === "ok" ? "warn" : state;
  connText.textContent = text;
  reloadBtn.hidden = state !== "stale";
}

async function checkHealth() {
  clearTimeout(healthTimer);
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), HEALTH_TIMEOUT_MS);
  try {
    const res = await fetch("/api/health", { cache: "no-store", signal: abort.signal });
    if (!res.ok) throw new Error(res.status);
    const h = await res.json();
    failures = 0;
    if (h.version !== VERSION) {
      setConn("stale", `Server updated to v${h.version}. Reload this page.`);
    } else if (h.stream === "off") {
      setConn("warn", "Connected. Save a destination to start the stream to the Core.");
    } else if (h.stream === "down") {
      setConn("warn", "Connected, but the stream to the Core isn't running. See the server log.");
    } else {
      setConn("ok", "Streaming to the Core");
    }
    if (!settingsLoaded && !settingsLoading) {
      // The page opened while the server was down: fill the form now it's back.
      loadSettings()
        .then(() => { if (statusEl.textContent === SETTINGS_ERROR) setStatus("Ready"); })
        .catch(() => {});
    }
  } catch (_) {
    failures += 1;
    if (failures < 3) setConn("warn", "Server not answering, retrying…");
    else setConn("down", "Can't reach the server");
  } finally {
    clearTimeout(timer);
    if (!document.hidden) healthTimer = setTimeout(checkHealth, HEALTH_EVERY_MS);
  }
}

// ---- Link latency: a ping over a WebSocket every 2 s while the page is showing -----

const rttEl = $("connRtt");
let link, linkTimer, linkRetry, rtts = [], outstanding = new Map(), linkPoor = false;

function showRtt() {
  if (!rtts.length) { rttEl.textContent = ""; return; }
  const got = rtts.filter((r) => r !== null);
  const lost = rtts.length - got.length;
  const last = [...rtts].reverse().find((r) => r !== null);
  let jitter = 0;  // mean change between consecutive round trips, as RTP measures it
  for (let i = 1; i < got.length; i++) jitter += Math.abs(got[i] - got[i - 1]);
  if (got.length > 1) jitter /= got.length - 1;
  const r = Math.round;
  rttEl.textContent = last === undefined ? "no answer"
    : `${r(last)} ms · jitter ${r(jitter)} ms` + (lost ? ` · ${lost} lost` : "");
  rttEl.title = "Round trip to the server over the last " + rtts.length + " pings";
  linkPoor = lost > 0 || last > RTT_SLOW_MS || jitter > RTT_SLOW_MS / 3;
  conn.dataset.state = linkPoor && connState === "ok" ? "warn" : connState;
}

function record(rtt) {
  rtts.push(rtt);
  if (rtts.length > RTT_SAMPLES) rtts.shift();
  showRtt();
}

function linkPing() {
  // A ping still unanswered after a full interval is counted as lost.
  const now = performance.now();
  for (const [t] of outstanding) if (now - t > PING_EVERY_MS) { outstanding.delete(t); record(null); }
  if (link?.readyState !== WebSocket.OPEN) return;
  outstanding.set(now, true);
  link.send(JSON.stringify({ type: "ping", t: now }));
}

function openLink() {
  clearTimeout(linkRetry);
  if (document.hidden || link) return;
  link = new WebSocket(WS_BASE + "/ws/ping");
  link.onopen = () => { linkPing(); linkTimer = setInterval(linkPing, PING_EVERY_MS); };
  link.onmessage = (e) => {
    let m = {};
    try { m = JSON.parse(e.data); } catch (_) {}
    if (m.type === "pong" && outstanding.delete(m.t)) record(performance.now() - m.t);
  };
  link.onclose = () => {
    clearInterval(linkTimer);
    link = null;
    outstanding.clear();
    if (!document.hidden) linkRetry = setTimeout(openLink, PING_EVERY_MS);
  };
}

function closeLink() {
  clearTimeout(linkRetry);
  clearInterval(linkTimer);
  if (link) { link.onclose = null; link.close(1000); link = null; }
  outstanding.clear();
  rtts = [];
  showRtt();
}

reloadBtn.addEventListener("click", () => {
  if (live && !confirm("You're live. Reloading ends the page. Reload anyway?")) return;
  location.reload();
});

// No polling while the phone isn't showing the page; check at once when it comes back.
// A hidden page can't be relied on to keep the mic running, and shouldn't hold it open:
// let it go, and the next tap reconnects.
document.addEventListener("visibilitychange", () => {
  clearTimeout(healthTimer);
  if (!document.hidden) { openLink(); return checkHealth(); }
  closeLink();
  if (connected) {
    disconnect(1001, true);
    setStatus("The mic was released while the page was hidden. Tap Go live to talk.");
  }
});

if (!navigator.mediaDevices?.getUserMedia) {
  setStatus("This page needs HTTPS to use the microphone.", "error");
  liveBtn.disabled = true;
}
loadSettings().catch(() => setStatus(SETTINGS_ERROR, "error"));
checkHealth();
openLink();

// ---- Tabs -----------------------------------------------------------------

const tabs = [...document.querySelectorAll('[role="tab"]')];

function showTab(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  });
  try { localStorage.setItem("pa.tab", tab.id); } catch (_) {}
}

tabs.forEach((t, i) => {
  t.addEventListener("click", () => showTab(t));
  t.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    const next = tabs[(i + step + tabs.length) % tabs.length];
    showTab(next);
    next.focus();
  });
});
try { const saved = $(localStorage.getItem("pa.tab")); if (tabs.includes(saved)) showTab(saved); } catch (_) {}
