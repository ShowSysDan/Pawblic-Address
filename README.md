<p align="center"><img src="static/logo.svg" width="112" alt="Pawblic Address logo"></p>

<h1 align="center">Pawblic Address</h1>

<p align="center"><b>PA</b> — turn any phone's web browser into a paging mic for a Q-SYS system.</p>

Pawblic Address streams a phone's microphone to a Q-SYS Core with as little delay as practical.
Open the page, tap **Go live**, talk.

```
Phone browser ──raw PCM over WebSocket──▶ Flask ──stdin──▶ ffmpeg ──L16 (or MP3) over RTP──▶ Q-SYS Media Stream Receiver
```

Proof of concept: one page, one button, one destination. The version is in the page footer;
see [CHANGELOG.md](CHANGELOG.md) for what changed.

## Requirements

- Python 3.10+
- ffmpeg on the PATH (tested with 6.1; needs `libmp3lame`)
- The phone and the server on a network that can reach the Core

## Setup

```bash
git clone https://github.com/ShowSysDan/Pawblic-Address.git && cd Pawblic-Address
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Phones only allow microphone access over HTTPS, so make a self-signed certificate once:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout key.pem -out cert.pem -subj "/CN=pawblic-address"
```

(`mkcert` gives you a cert the phone trusts without a warning, if you'd rather.)

## Run

```bash
python app.py
```

Then on the phone open `https://<server-ip>:7100`, accept the certificate warning once, and tap **Go live**.

Under the logo, a dot shows whether the page can reach the server. The page asks
`/api/health` every 5 seconds: green means connected, amber means it missed a check, and
red means it can't reach the server. If the server has been updated since the page was
opened, the indicator says so and offers **Reload**.

Settings (the Q-SYS destination and the syslog server) are set on the page and stored in
`settings.json` next to `app.py`. Environment variables:

| Variable        | Default        | Purpose                        |
|-----------------|----------------|--------------------------------|
| `PORT`          | `7100`         | HTTPS port for the web page    |
| `FFMPEG_BIN`    | `ffmpeg`       | Path to the ffmpeg binary      |
| `SETTINGS_FILE` | `settings.json`| Where settings are stored      |

To run it as a service, have the service manager stop it with SIGTERM (systemd and Docker do
by default). On SIGTERM the server ends any live page, stops ffmpeg and logs `service_stop`.

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

## Syslog

Set **Syslog → Server** (and **Port**, default 514) on the page and save; **Send syslog test**
saves and then sends a `syslog_test` message so you can check it arrives. Leave the server
blank to turn syslog off. Use an IP address if you can: a hostname is looked up once, when
settings are saved or the server starts.

Messages are RFC 5424 over UDP, facility local0, with the event name as the MSGID and
`key=value` pairs in the message:

```
<134>1 2026-10-07T22:48:50.440Z pa-server pawblic-address 766 page_stop - event=page_stop client=10.0.4.21 reason=stopped duration_s=42.3 audio_s=42.2 dropped_s=0.0 ffmpeg_exit=0
```

A **page** is one Go live → Stop session.

| Event | Severity | When | Fields |
|-------|----------|------|--------|
| `page_start` | info | Someone goes live | `client`, `codec`, `dest`, `sample_rate`, `ffmpeg_pid`, `ua` |
| `page_stop` | info | A page ends, however it ends | `client`, `reason`, `duration_s` (total page time), `audio_s` (audio that reached the server), `dropped_s` (dropped because ffmpeg fell behind), `ffmpeg_exit` |
| `page_busy` | warning | Someone tried to go live while another phone was live | `client`, `live_client` |
| `page_rejected` | warning | Refused before going live: another site, or a bad or missing start message | `client`, `reason`, `origin` |
| `page_error` | error | ffmpeg failed during a page | `client`, `error` |
| `settings_changed` | info | Settings saved with changes | `client`, then each changed setting as `"old -> new"` |
| `settings_rejected` | warning | Invalid settings, or a save from another site | `client`, `error`, `origin` |
| `console_open` | info | Someone opened the page | `client` |
| `relay_reaped` | error | The watchdog stopped an ffmpeg nobody was feeding (shouldn't happen) | `pid`, `idle_s`, `dest` |
| `syslog_test` | info | **Send syslog test** | `client`, `version` |
| `service_start` / `service_stop` | info | Server starts / stops | `version`, `port`, `https`, `pid` / `version`, `uptime_s` |

`page_stop` reasons: `stopped` (Stop tapped), `left` (tab closed or navigated away),
`disconnected` (connection dropped), `timeout` (no audio for 5 s, e.g. the phone locked or
left Wi-Fi), `error` (ffmpeg failed), `shutdown` (server stopped). When `audio_s` is well
below `duration_s`, the phone's Wi-Fi was losing audio.

Other server log lines at warning or above (ffmpeg errors, for example) are forwarded too,
with MSGID `log`.

## When something goes wrong mid-page

Every ffmpeg process belongs to one page and is stopped when that page ends, however it ends.
`tests/soak.py` checks all of these against the real server:

| What happens | What the server does |
|--------------|----------------------|
| Phone taps Stop, closes the tab, or drops the connection | Page ends at once; ffmpeg is flushed and stopped |
| Phone goes quiet (screen locked, off Wi-Fi, no TCP close) | Page ends after 5 s without audio and the line is free again |
| Phone connects but never starts streaming | Refused after 5 s |
| ffmpeg exits (bad destination, crash) | The error is shown on the phone and logged as `page_error` |
| ffmpeg hangs and stops reading | Detected within about 2 s, then ffmpeg is killed (SIGKILL if it ignores SIGTERM) |
| A page's thread gets stuck anyway | A watchdog stops any ffmpeg nobody has fed for 15 s (`relay_reaped`) |
| Server gets SIGTERM / Ctrl-C | Live page ends (`reason=shutdown`) and ffmpeg is stopped before exit |
| Server is killed outright (SIGKILL, crash) | ffmpeg sees its input close and exits by itself |

Writing audio to ffmpeg never blocks the WebSocket: it goes through a short queue (about
0.5 s), and if ffmpeg falls behind, audio is dropped and counted in `dropped_s` rather than
delaying the stream.

## Security

What's in place:

- The audio WebSocket and the settings API refuse requests from other websites (the
  `Origin` header must match the page). Without this, any web page a staff member opened
  could put audio on the PA or change the destination through their browser.
- Settings must be sent as JSON, which other sites can't do without permission.
- Settings are validated: the Core and syslog addresses must be plain IPs or hostnames, so
  nothing can be slipped into ffmpeg's arguments. ffmpeg is run without a shell.
  A hand-edited `settings.json` is checked the same way, and it's written atomically.
- Request bodies are capped at 16 KB and WebSocket messages at 64 KB.
- Strict Content-Security-Policy, no framing, `nosniff`, no referrer.
- Values in log lines are sanitised, so nothing a client sends can forge extra log lines.

What isn't yet, so plan around it:

- **There is no login.** Anyone who can reach the page can go live and change the settings,
  including where audio and syslog go. Keep the server on a network only staff can reach
  (a VLAN or firewall rule), until a password is added.
- Flask's built-in server starts a thread per connection, without a limit. That's fine on a
  staff network but shouldn't face the internet.
- A self-signed certificate teaches people to click through warnings. `mkcert` or an
  internal CA is better.
- Syslog over UDP is neither encrypted nor authenticated (as is usual for syslog).

## Troubleshooting

- **Red "Can't reach the server"** — the server isn't running, or the phone is on a network that can't reach it.
- **"Server updated… Reload"** — the server was upgraded since the page was opened; tap Reload.
- **No syslog messages** — use **Send syslog test**; check the server's firewall allows UDP out and the syslog server listens on UDP.
- **"This page needs HTTPS"** — you opened `http://`, or `cert.pem`/`key.pem` are missing so the server fell back to plain HTTP.
- **Microphone blocked** — check the site permission in the phone browser; on iOS also Settings → Safari → Microphone.
- **Live, but the Core hears nothing** — confirm the Core IP and that the receiver's port matches. Watch the server log: ffmpeg errors are printed there and sent to the page.
- **"Another phone is already live"** — the POC allows one stream at a time. A phone that silently dropped off frees the line after 5 s.
- **Audio drifts behind over time** — lower the Core's Stream Buffer; on a poor link lower `MAX_BACKLOG_MS`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # unit and integration tests, about 25 s (Linux, needs ffmpeg for one test)
python tests/soak.py        # soak and leak test against the real server and ffmpeg, about 4 min
```

`tests/soak.py` runs hundreds of pages through every way a page can end. It reports the
server's memory, threads, open files and ffmpeg processes, and kills the server during a
page to check ffmpeg doesn't outlive it.

## Layout

```
app.py                       Flask routes + WebSocket audio ingest, security checks
relay.py                     ffmpeg process per page (command, writer queue, stop, watchdog)
events.py                    event logging and syslog (RFC 5424 over UDP)
settings.py                  settings.json load/save/validate
version.py                   the version number (bump it with every change)
templates/index.html         the page
static/app.js                mic capture, WebSocket, settings UI
static/pcm-worklet.js        AudioWorklet: Float32 → Int16 PCM chunks
static/logo.svg              logo (also the favicon)
static/apple-touch-icon.png  home-screen icon for phones
tests/                       pytest suite, fake ffmpegs, soak.py
CHANGELOG.md                 what changed in each version
CLAUDE.md                    project rules
```

## Next

- Level meter on the page
- Several saved destinations
- Password on the page (see Security)
- gunicorn + systemd / Dockerfile
- Opus over RTP if the Core gains support for it
