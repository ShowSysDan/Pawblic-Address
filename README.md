# qsys-mic-relay

Streams a phone's microphone to a Q-SYS Core with as little delay as practical.

```
Phone browser ──raw PCM over WebSocket──▶ Flask ──stdin──▶ ffmpeg ──L16 (or MP3) over RTP──▶ Q-SYS Media Stream Receiver
```

Proof of concept: one page, one button, one destination.

## Requirements

- Python 3.10+
- ffmpeg on the PATH (tested with 6.1; needs `libmp3lame`)
- The phone and the server on a network that can reach the Core

## Setup

```bash
git clone <this repo> && cd qsys-mic-relay
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Phones only allow microphone access over HTTPS, so make a self-signed certificate once:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout key.pem -out cert.pem -subj "/CN=mic-relay"
```

(`mkcert` gives you a cert the phone trusts without a warning, if you'd rather.)

## Run

```bash
python app.py
```

Then on the phone open `https://<server-ip>:5000`, accept the certificate warning once, and tap **Go live**.

Settings are stored in `settings.json` next to `app.py`. Environment variables:

| Variable        | Default        | Purpose                        |
|-----------------|----------------|--------------------------------|
| `PORT`          | `5000`         | HTTPS port for the web page    |
| `FFMPEG_BIN`    | `ffmpeg`       | Path to the ffmpeg binary      |
| `SETTINGS_FILE` | `settings.json`| Where settings are stored      |

## Q-SYS side

Add a **Media Stream Receiver** to the design and set its stream to `rtp://:<port>` (the port
you typed on the page, e.g. `rtp://:4848`). The Core only needs the port; ffmpeg pushes to the
Core's IP. Set the receiver's **Stream Buffer** as low as it will go — this is the biggest latency
knob you have.

Codec options on the page:

| Codec         | What ffmpeg sends                         | Why pick it                                 |
|---------------|-------------------------------------------|---------------------------------------------|
| PCM (default) | L16 stereo 44.1 kHz (static PT 10)        | No encoder delay, lowest latency, ~1.4 Mbps |
| MP3           | Mono MP3, one frame per RTP packet (PT 14)| If bandwidth to the Core is tight           |

Both are things the receiver decodes without an SDP file.

## Where the latency goes

Measured on the server (first PCM byte in → first RTP packet out): MP3 ≈ 80 ms, PCM ≈ 20 ms.
End to end you also pay for the phone's audio stack, Wi-Fi, and the Core's receive buffer.

| Knob                                  | Where                        | Default |
|---------------------------------------|------------------------------|---------|
| Chunk size sent from the phone        | `CHUNK_MS` in `static/app.js`| 20 ms   |
| Drop-instead-of-queue threshold       | `MAX_BACKLOG_MS` in `app.js` | 100 ms  |
| PCM vs MP3                            | Settings on the page         | PCM     |
| Receiver Stream Buffer                | Q-SYS Designer               | —       |

Things already done for you, don't undo them:

- The browser sends raw 16-bit PCM from an `AudioWorklet`. MediaRecorder was deliberately avoided; it buffers into container chunks and iOS's output isn't streamable mid-recording.
- Browser echo cancellation, noise suppression and AGC are off (they add delay and colour a PA feed).
- ffmpeg runs with `-probesize 32 -analyzeduration 0`. Without them it buffers about a second of audio before encoding. `-fflags nobuffer` is **not** used: on ffmpeg 6.x it silently breaks raw PCM input.
- RTP packet size is calculated so MP3 goes out one frame per packet instead of ffmpeg packing three frames (~50 ms) into each one.
- If the Wi-Fi link stalls, the phone drops audio instead of queueing it, so the stream stays live rather than drifting behind.

## Troubleshooting

- **"This page needs HTTPS"** — you opened `http://`, or `cert.pem`/`key.pem` are missing so the server fell back to plain HTTP.
- **Microphone blocked** — check the site permission in the phone browser; on iOS also Settings → Safari → Microphone.
- **Live, but the Core hears nothing** — confirm the Core IP and that the receiver's port matches. Watch the server log: ffmpeg errors are printed there and sent to the page.
- **"Another phone is already live"** — the POC allows one stream at a time.
- **Audio drifts behind over time** — lower the Core's Stream Buffer; on a poor link lower `MAX_BACKLOG_MS`.

## Layout

```
app.py                  Flask routes + WebSocket audio ingest
relay.py                ffmpeg process per session (command building, start/write/stop)
settings.py             settings.json load/save/validate
templates/index.html    the page
static/app.js           mic capture, WebSocket, settings UI
static/pcm-worklet.js   AudioWorklet: Float32 → Int16 PCM chunks
```

## Next

- Level meter on the page
- Several saved destinations
- Password on the page
- gunicorn + systemd / Dockerfile
- Opus over RTP if the Core gains support for it
