// Two ways to page, sharing one mic:
//
// Announce (the main one): record on the phone, listen back, then send the recording to
// the server, which plays it whole into the stream to the Core. Nothing is live, so the
// network can't add delay or dropouts to what the room hears.
//
// Live: Mic -> AudioWorklet (48 kHz Int16 PCM) -> WebSocket -> server -> always-on stream.
// Tuned for latency: no MediaRecorder, no browser DSP, small chunks, and chunks are dropped
// rather than queued if the link stalls. The first tap opens the mic and connects; after
// that the button only mutes and unmutes, which is instant.
//
// The mic is released when it isn't needed (recording stopped, Release tapped) and when the
// page is hidden.

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
const MAX_RECORD_S = 120;      // stream.MAX_ANNOUNCE_S on the server
const MIN_RECORD_S = 0.3;      // stream.MIN_ANNOUNCE_S
const ANNOUNCE_POLL_MS = 500;  // while a sent announcement waits or plays

const VERSION = document.documentElement.dataset.version;
const WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

const $ = (id) => document.getElementById(id);
const liveBtn = $("liveBtn");
const statusEl = $("status");
const releaseBtn = $("releaseBtn");
const sidetoneBox = $("sidetone");
const holdBox = $("holdToTalk");
const meters = [...document.querySelectorAll(".meter")];

let ctx, stream, source, worklet, silent, sidetone, ws, wakeLock, pingTimer;
let micOpening = null; // the promise while the mic is being opened
let connected = false; // live: mic open and the server said ready
let live = false;      // holding the floor
let sending = false;   // audio is going out (from the tap, before the server confirms)
let recording = false; // collecting a take for an announcement

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

function render() {
  document.body.classList.toggle("live", live);
  liveBtn.classList.toggle("on", live);
  if (holdBox.checked) liveBtn.textContent = !connected ? "Turn on mic" : live ? "Release to mute" : "Hold to talk";
  else liveBtn.textContent = live ? "Mute" : "Go live";
  liveBtn.setAttribute("aria-pressed", String(live));
  releaseBtn.hidden = !connected;
  $("liveMeter").hidden = !connected;
  syncSidetone();
}

// ---- Level meter: the mic's peak, live, muted or recording, so a talker can see it's working --

let shown = 0;  // dB shown, falls back slowly like a real meter

function showLevel(peak) {
  const db = peak > 0 ? 20 * Math.log10(peak) : -100;
  shown = Math.max(db, shown - 3);  // ~60 dB/s fall at 20 updates a second
  const pct = Math.max(0, Math.min(100, (shown + 60) / 60 * 100));
  const level = shown > -3 ? "clip" : shown > -12 ? "hot" : "ok";
  for (const m of meters) {
    m.firstElementChild.style.width = pct + "%";
    m.dataset.level = level;
  }
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

// ---- The mic, shared by recording and live ------------------------------------

function micError(err) {
  return err.name === "NotAllowedError"
    ? "Microphone access was blocked. Allow it in the browser and try again."
    : err.message || String(err);
}

// The worklet only produces audio while something wants it.
function syncWorklet() {
  worklet?.port.postMessage({ sending: sending || recording });
}

function onMicMessage(e) {
  if (!(e.data instanceof ArrayBuffer)) return showLevel(e.data.peak);
  if (recording) addToTake(e.data);
  if (!sending || ws?.readyState !== WebSocket.OPEN) return;
  if (ws.bufferedAmount > RATE * 2 * MAX_BACKLOG_MS / 1000) return; // stay live, skip this chunk
  ws.send(e.data);
}

function onMicLost() {
  if (recording) stopRecording("The microphone was taken away (a call?). Recording stopped.");
  if (connected) { setStatus("The microphone was taken away (a call?). Tap to go live again.", "error"); disconnect(1000, true); }
}

// Must be called from a tap, so iOS lets the audio run.
function openMic() {
  if (ctx) return Promise.resolve();
  micOpening ??= startMic().finally(() => { micOpening = null; });
  return micOpening;
}

async function startMic() {
  let c;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Browser DSP adds delay and colours a PA feed; keep the mic raw.
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    stream.getAudioTracks()[0].onended = onMicLost;
    c = new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
    await c.resume();
    await c.audioWorklet.addModule("/static/pcm-worklet.js?v=" + VERSION);
    source = c.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(c, "pcm-sender", {
      numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
      processorOptions: { chunkSamples: CHUNK_SAMPLES },
    });
    worklet.port.onmessage = onMicMessage;
    // Silent path to the output keeps the worklet scheduled on every browser.
    silent = c.createGain();
    silent.gain.value = 0;
    source.connect(worklet).connect(silent).connect(c.destination);
    sidetone = c.createGain();
    sidetone.gain.value = 0;
    source.connect(sidetone).connect(c.destination);
    ctx = c;
  } catch (err) {
    stream?.getTracks().forEach((t) => t.stop());
    c?.close().catch(() => {});
    stream = source = worklet = silent = sidetone = null;
    throw err;
  }
}

// Let the mic go once neither recording nor live needs it.
async function releaseMicIfIdle() {
  if (recording || connected || micOpening || !ctx) return;
  try { source?.disconnect(); worklet?.disconnect(); silent?.disconnect(); sidetone?.disconnect(); } catch (_) {}
  stream?.getTracks().forEach((t) => t.stop());
  const c = ctx;
  ctx = stream = source = worklet = silent = sidetone = null;
  meters.forEach((m) => { m.firstElementChild.style.width = "0"; });
  try { await c.close(); } catch (_) {}
}

// ---- Announce: record, listen back, play on the PA ------------------------------

const recBtn = $("recBtn");
const recTime = $("recTime");
const annStatus = $("annStatus");
const takeBox = $("take");
const listenBtn = $("listenBtn");
const playBtn = $("playBtn");
const stopAnnBtn = $("stopAnnBtn");

let take = null;        // the finished recording: an array of Int16 PCM ArrayBuffers
let takeBytes = 0;
let recChunks = [];
let recBytes = 0;
let recTimer;
let playCtx, preview;   // listening back on this phone
let sentId = null;      // the announcement we're following, and its poll timer
let annTimer;

function setAnnStatus(text, kind = "") {
  annStatus.textContent = text;
  annStatus.dataset.kind = kind;
}

const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const seconds = (bytes) => bytes / 2 / RATE;

function renderTake() {
  recBtn.classList.toggle("on", recording);
  recBtn.textContent = recording ? "Stop" : take ? "Record again" : "Record";
  recBtn.setAttribute("aria-pressed", String(recording));
  $("recMeter").hidden = !recording;
  takeBox.hidden = recording || !take;
  listenBtn.textContent = preview ? "Stop listening" : "Listen";
  const s = recording ? seconds(recBytes) : take ? seconds(takeBytes) : 0;
  recTime.textContent = recording ? `${clock(s)} / ${clock(MAX_RECORD_S)}` : take ? clock(s) : "";
}

function addToTake(buf) {
  recChunks.push(buf);
  recBytes += buf.byteLength;
  if (seconds(recBytes) >= MAX_RECORD_S) {
    stopRecording(`Stopped at the ${MAX_RECORD_S / 60}-minute limit. Listen, then play it on the PA.`);
  }
}

async function startRecording() {
  stopPreview();
  recBtn.disabled = true;
  setAnnStatus("Opening the microphone…");
  try {
    await openMic();
  } catch (err) {
    setAnnStatus(micError(err), "error");
    return;
  } finally {
    recBtn.disabled = false;
  }
  recChunks = [];
  recBytes = 0;
  recording = true;
  syncWorklet();
  holdWakeLock(true);
  setAnnStatus("Recording. Say the announcement, then tap Stop.", "live");
  recTimer = setInterval(renderTake, 200);
  renderTake();
}

function stopRecording(msg) {
  if (!recording) return;
  recording = false;
  syncWorklet();
  clearInterval(recTimer);
  if (!live) holdWakeLock(false);
  if (seconds(recBytes) < MIN_RECORD_S) {
    setAnnStatus("That was too short to keep. Tap Record and try again.", "error");
  } else {
    take = recChunks;
    takeBytes = recBytes;
    setAnnStatus(msg || "Listen to check it, then play it on the PA.");
  }
  recChunks = [];
  recBytes = 0;
  renderTake();
  releaseMicIfIdle();
}

recBtn.addEventListener("click", () => (recording ? stopRecording() : startRecording()));

function stopPreview() {
  if (!preview) return;
  preview.onended = null;
  try { preview.stop(); } catch (_) {}
  preview = null;
  renderTake();
}

listenBtn.addEventListener("click", async () => {
  if (preview) return stopPreview();
  if (!take) return;
  playCtx ??= new AudioContext({ sampleRate: RATE });
  await playCtx.resume();
  const pcm = new Int16Array(takeBytes / 2);
  let at = 0;
  for (const b of take) { pcm.set(new Int16Array(b), at); at += b.byteLength / 2; }
  const buf = playCtx.createBuffer(1, pcm.length, RATE);
  const out = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 0x8000;
  preview = playCtx.createBufferSource();
  preview.buffer = buf;
  preview.connect(playCtx.destination);
  preview.onended = () => { preview = null; renderTake(); };
  preview.start();
  renderTake();
});

playBtn.addEventListener("click", async () => {
  if (!take) return;
  stopPreview();
  playBtn.disabled = true;
  setAnnStatus("Sending…");
  try {
    const res = await fetch("/api/announce", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: new Blob(take, { type: "application/octet-stream" }),
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(data.error || `Server error (${res.status})`);
    sentId = data.id;
    showSent(data.ahead ? { queued: [data.id], ahead: data.ahead } : { live: data.live });
    followAnnouncement();
  } catch (err) {
    setAnnStatus(err.message === "Failed to fetch" ? "Couldn't reach the server. Try again." : err.message, "error");
  } finally {
    playBtn.disabled = false;
  }
});

function showSent({ queued, ahead, live: someoneLive }) {
  if (ahead) setAnnStatus(`Sent. It plays after ${ahead} other announcement${ahead === 1 ? "" : "s"}.`, "live");
  else if (someoneLive) setAnnStatus("Sent. It plays when the live phone mutes.", "live");
  else setAnnStatus("Sent. Starting…", "live");
}

// Follow the announcement we sent until it has played.
async function followAnnouncement() {
  clearTimeout(annTimer);
  if (sentId === null) return;
  try {
    const st = await (await fetch("/api/announce", { cache: "no-store" })).json();
    stopAnnBtn.hidden = !st.playing && !st.queued.length;
    if (st.playing?.id === sentId) {
      setAnnStatus(`Playing on the PA: ${clock(st.playing.played_s)} of ${clock(st.playing.audio_s)}`, "live");
    } else if (st.queued.includes(sentId)) {
      const ahead = st.queued.indexOf(sentId) + (st.playing ? 1 : 0);
      if (ahead) showSent({ ahead });
      else if (st.live || st.held) showSent({ live: true });
    } else {
      const how = st.finished?.[sentId];
      setAnnStatus(how === "played" ? "Played on the PA."
        : how ? "Stopped before it finished." : "Done.", how && how !== "played" ? "error" : "");
      sentId = null;
      return;
    }
  } catch (_) {}
  annTimer = setTimeout(followAnnouncement, ANNOUNCE_POLL_MS);
}

stopAnnBtn.addEventListener("click", async () => {
  try {
    await postJSON("/api/announce/stop");
    stopAnnBtn.hidden = true;
    if (sentId === null) setAnnStatus("Stopped.");
  } catch (err) {
    setAnnStatus(err.message, "error");
  }
});

renderTake();

// ---- Live -----------------------------------------------------------------------

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
  syncWorklet();
}

async function connect() {
  setStatus("Asking for the microphone…");
  await openMic();

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
  if (ws) { ws.onclose = null; if (ws.readyState <= WebSocket.OPEN) ws.close(code, "release"); }
  ws = pingTimer = null;
  await releaseMicIfIdle();
  await holdWakeLock(false);
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
    setStatus(micError(err), "error");
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

// Space bar on a computer, unless typing in a field, and only on the Live tab.
const typing = () => /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(document.activeElement?.tagName);
const onLiveTab = () => !$("panelLive").hidden;
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || !holdBox.checked || !connected || typing() || !onLiveTab()) return;
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
    if (sentId === null) stopAnnBtn.hidden = !h.announcements;
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
  if (take && !confirm("Reloading loses the recording on this phone. Reload anyway?")) return;
  location.reload();
});

// No polling while the phone isn't showing the page; check at once when it comes back.
// A hidden page can't be relied on to keep the mic running, and shouldn't hold it open:
// let it go, and the next tap reconnects.
document.addEventListener("visibilitychange", () => {
  clearTimeout(healthTimer);
  if (!document.hidden) { openLink(); return checkHealth(); }
  closeLink();
  stopPreview();
  if (recording) stopRecording("Recording stopped when the page was hidden. Listen to check it.");
  if (connected) {
    disconnect(1001, true);
    setStatus("The mic was released while the page was hidden. Tap Go live to talk.");
  }
});

if (!navigator.mediaDevices?.getUserMedia) {
  setStatus("This page needs HTTPS to use the microphone.", "error");
  setAnnStatus("This page needs HTTPS to use the microphone.", "error");
  liveBtn.disabled = recBtn.disabled = true;
}
loadSettings().catch(() => setStatus(SETTINGS_ERROR, "error"));
checkHealth();
openLink();

// ---- Tabs -----------------------------------------------------------------

const tabs = [...document.querySelectorAll('[role="tab"]')];

function showTab(tab) {
  // Leaving a tab stops what it was doing with the mic.
  if (tab.id !== "tabLive" && (live || sending)) mute();
  if (tab.id !== "tabAnnounce") { stopRecording(); stopPreview(); }
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
