// Mic -> AudioWorklet (Int16 PCM) -> WebSocket -> server.
// Everything here is tuned for latency: no MediaRecorder, no browser DSP,
// small chunks, and chunks are dropped rather than queued if the link stalls.

const CHUNK_MS = 20;          // PCM per WebSocket message. 10 is fine on a solid LAN.
const MAX_BACKLOG_MS = 100;   // If this much is unsent, drop new chunks to stay live.

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
  await ctx.audioWorklet.addModule("/static/pcm-worklet.js");

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
  if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
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

const fields = ["ip", "port", "codec", "bitrate"];
const saveNote = $("saveNote");

function syncCodecRow() {
  $("bitrateRow").hidden = $("codec").value !== "mp3";
}

async function loadSettings() {
  const cfg = await (await fetch("/api/settings")).json();
  fields.forEach((f) => { $(f).value = cfg[f]; });
  syncCodecRow();
}

$("codec").addEventListener("change", syncCodecRow);

$("saveBtn").addEventListener("click", async () => {
  const body = Object.fromEntries(fields.map((f) => [f, $(f).value]));
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    saveNote.textContent = data.error;
    saveNote.dataset.kind = "error";
    return;
  }
  fields.forEach((f) => { $(f).value = data[f]; });
  saveNote.textContent = live ? "Saved. Applies the next time you go live." : "Saved.";
  saveNote.dataset.kind = "";
});

if (!navigator.mediaDevices?.getUserMedia) {
  setStatus("This page needs HTTPS to use the microphone.", "error");
  liveBtn.disabled = true;
}
loadSettings().catch(() => setStatus("Could not load settings from the server", "error"));
