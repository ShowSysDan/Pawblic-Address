// Mic -> AudioWorklet (Int16 PCM) -> WebSocket -> server.
// Everything here is tuned for latency: no MediaRecorder, no browser DSP,
// small chunks, and chunks are dropped rather than queued if the link stalls.

const CHUNK_MS = 20;          // PCM per WebSocket message. 10 is fine on a solid LAN.
const MAX_BACKLOG_MS = 100;   // If this much is unsent, drop new chunks to stay live.
const HEALTH_EVERY_MS = 5000; // How often to check the server is answering.
const HEALTH_TIMEOUT_MS = 3000;

const VERSION = document.documentElement.dataset.version;

const $ = (id) => document.getElementById(id);
const liveBtn = $("liveBtn");
const statusEl = $("status");

let ctx, stream, source, worklet, mute, ws, wakeLock;
let live = false;

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function setLive(on) {
  live = on;
  document.body.classList.toggle("live", on);
  liveBtn.textContent = on ? "Stop" : "Go live";
  liveBtn.setAttribute("aria-pressed", String(on));
}

async function start() {
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

  // Must be created inside the tap handler so iOS lets it run.
  ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
  await ctx.resume();
  await ctx.audioWorklet.addModule("/static/pcm-worklet.js?v=" + VERSION);

  setStatus("Connecting…");
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  ws = new WebSocket(proto + location.host + "/ws/audio");
  ws.binaryType = "arraybuffer";
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("Could not reach the server"));
  });
  // The phone may not honour 48 kHz; tell the server what we actually got.
  ws.send(JSON.stringify({ sampleRate: ctx.sampleRate }));

  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "live") setStatus("Live, " + m.msg, "live");
    else if (m.type === "error") { setStatus(m.msg, "error"); stop(true); }
  };
  ws.onclose = () => { if (live) { setStatus("Server closed the connection", "error"); stop(true); } };

  const backlogBytes = ctx.sampleRate * 2 * MAX_BACKLOG_MS / 1000;
  source = ctx.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(ctx, "pcm-sender", {
    numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
    processorOptions: { chunkMs: CHUNK_MS },
  });
  worklet.port.onmessage = (e) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > backlogBytes) return; // stay live, skip this chunk
    ws.send(e.data);
  };
  // Silent path to the output keeps the worklet scheduled on every browser.
  mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(worklet).connect(mute).connect(ctx.destination);

  try { wakeLock = await navigator.wakeLock?.request("screen"); } catch (_) {}
  setLive(true);
}

async function stop(fromError = false) {
  setLive(false);
  try { source?.disconnect(); worklet?.disconnect(); mute?.disconnect(); } catch (_) {}
  stream?.getTracks().forEach((t) => t.stop());
  // 1000 tells the server this was a deliberate Stop, not a dropped connection.
  if (ws && ws.readyState <= WebSocket.OPEN) ws.close(1000, "stop");
  try { await ctx?.close(); } catch (_) {}
  try { await wakeLock?.release(); } catch (_) {}
  ctx = stream = source = worklet = mute = ws = wakeLock = null;
  if (!fromError) setStatus("Ready");
}

liveBtn.addEventListener("click", async () => {
  if (live) return stop();
  liveBtn.disabled = true;
  try {
    await start();
  } catch (err) {
    const msg = err.name === "NotAllowedError"
      ? "Microphone access was blocked. Allow it in the browser and try again."
      : err.message || String(err);
    setStatus(msg, "error");
    await stop(true);
  } finally {
    liveBtn.disabled = false;
  }
});

// ---- Settings -------------------------------------------------------------

const fields = ["ip", "port", "codec", "bitrate", "syslog_host", "syslog_port"];
const saveNote = $("saveNote");
const SETTINGS_ERROR = "Could not load settings from the server";
let settingsLoaded = false;
let settingsLoading = false;

function syncCodecRow() {
  $("bitrateRow").hidden = $("codec").value !== "mp3";
}

function note(text, kind = "") {
  saveNote.textContent = text;
  saveNote.dataset.kind = kind;
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
    note(live ? "Saved. Destination changes apply the next time you go live." : "Saved.");
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

function setConn(state, text) {
  conn.dataset.state = state;
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
    } else {
      setConn("ok", "Connected to the server");
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

reloadBtn.addEventListener("click", () => {
  if (live && !confirm("You're live. Reloading ends the page. Reload anyway?")) return;
  location.reload();
});

// No polling while the phone isn't showing the page; check at once when it comes back.
document.addEventListener("visibilitychange", () => {
  clearTimeout(healthTimer);
  if (!document.hidden) checkHealth();
});

if (!navigator.mediaDevices?.getUserMedia) {
  setStatus("This page needs HTTPS to use the microphone.", "error");
  liveBtn.disabled = true;
}
loadSettings().catch(() => setStatus(SETTINGS_ERROR, "error"));
checkHealth();
