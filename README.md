<p align="center"><img src="static/logo.svg" width="112" alt="Pawblic Address logo"></p>

<h1 align="center">Pawblic Address</h1>

<p align="center"><b>PA</b> — turn any phone's web browser into a paging mic for a Q-SYS system.</p>

Pawblic Address streams a phone's microphone to a Q-SYS Core with as little delay as practical.
Open the page, tap **Go live**, talk, tap **Mute**.

```
Phone browser ──raw PCM over WebSocket──▶ Flask ──jitter buffer──▶ ffmpeg ──L16 (or MP3) over RTP──▶ Q-SYS Media Stream Receiver
                                                 (server clock, always on; silence when nobody talks)
```

The stream to the Core runs all the time, so going live never waits for ffmpeg to start or
for the Core to lock on to a new stream. The phone's button only decides whose audio goes
into it.

Proof of concept: one page, one button, one destination. The version is in the page footer;
see [CHANGELOG.md](CHANGELOG.md) for what changed.

## Install (Linux server, runs as a service)

Pawblic Address installs into your home folder and runs as a systemd service under your
user account. It starts at boot and restarts itself if it crashes. These steps are for
Ubuntu, Debian or Raspberry Pi OS. Other systemd distributions work too, with their own
package names.

You need:
- a server the phones can reach and that can reach the Q-SYS Core;
- Python 3.10 or newer, ffmpeg (with `libmp3lame`, as Debian's and Ubuntu's are) and openssl;
- an account that can use `sudo`, to install the service file.

**1. Install the system packages**

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg openssl
```

**2. Get the app into your home folder.** Do this as the user the service will run as, not
as root:

```bash
cd ~
git clone https://github.com/ShowSysDan/Pawblic-Address.git
cd Pawblic-Address
```

**3. Install and start the service**

```bash
./deploy/install-service.sh
```

It asks for your password once, for `sudo`, and then:
1. creates `~/Pawblic-Address/.venv` and installs the Python packages;
2. makes a self-signed HTTPS certificate (`cert.pem`, `key.pem`) if there isn't one already.
   Phones only allow the microphone over HTTPS;
3. installs `/etc/systemd/system/pawblic-address.service` from
   `deploy/pawblic-address.service`, set up to run as you from this folder;
4. starts the service, enables it at boot, checks it answers, and prints the address to
   open.

To use a port other than 7100, run `PORT=8443 ./deploy/install-service.sh`.

**4. Open the port if the server has a firewall**, e.g. `sudo ufw allow 7100/tcp`.

**5. On a phone, open `https://<server-ip>:7100`.** Accept the certificate warning once,
enter the Core's IP and port under **Destination**, tap **Save changes**, then **Go live**.
(`mkcert` or an internal CA gives you a certificate phones trust without the warning;
replace `cert.pem` and `key.pem` and restart.)

### Managing the service

| To | Run |
|----|-----|
| See if it's running | `systemctl status pawblic-address` |
| Watch the log | `journalctl -u pawblic-address -f` |
| Restart | `sudo systemctl restart pawblic-address` |
| Stop / start | `sudo systemctl stop pawblic-address` / `sudo systemctl start pawblic-address` |
| Don't start at boot | `sudo systemctl disable pawblic-address` |

Stopping ends any live page cleanly: the server stops ffmpeg and logs
`page_stop reason=shutdown`, `stream_stop` and `service_stop`.

### Updating

```bash
cd ~/Pawblic-Address
git pull
./deploy/install-service.sh
```

Re-running the install script updates the Python packages and the service file, then restarts
the service. Your `settings.json` and certificate are kept. Phones that still have the page
open show **Server updated… Reload**.

### Uninstalling

```bash
sudo systemctl disable --now pawblic-address
sudo rm /etc/systemd/system/pawblic-address.service
sudo systemctl daemon-reload
rm -rf ~/Pawblic-Address
```

### Installing by hand

If you'd rather not use the script, here's what it does:

```bash
cd ~/Pawblic-Address
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout key.pem -out cert.pem -subj "/CN=pawblic-address"
chmod 600 key.pem
sed -e "s|@USER@|$USER|g" -e "s|@APP_DIR@|$PWD|g" -e "s|@PORT@|7100|g" \
  deploy/pawblic-address.service | sudo tee /etc/systemd/system/pawblic-address.service
sudo systemctl daemon-reload
sudo systemctl enable --now pawblic-address
```

### Running in a terminal instead

For a quick test, or on a machine without systemd (Windows: `.venv\Scripts\activate`):

```bash
cd ~/Pawblic-Address
source .venv/bin/activate      # after creating it as above
python app.py                  # Ctrl-C to stop
```

Without `cert.pem` and `key.pem` it serves plain HTTP, and phones won't allow the microphone.

## Using it

The page has two tabs: **Talk** and **Settings** (destination, safety, syslog).

- **Go live** the first time opens the mic and connects (a few hundred ms; the phone may
  ask for mic permission). After that, **Mute** and **Go live** are instant: the mic stays
  open while the page is showing, and nothing is sent while muted.
- **Hold to talk** (per phone): hold the button to talk, let go to mute, like a paging mic.
  On a computer the space bar works too. Browsers only let a page open the mic from a
  completed tap, so with this on the first tap just turns the mic on.
- A level meter under the button shows the mic is picking you up, live or muted.
- The mic is let go when the page is hidden (screen locked, another app) or when you tap
  **Release the mic**. The next tap connects again.
- One phone talks at a time. Others can stay connected and muted; if one tries to go live
  while someone is talking it's told so, and can try again as soon as they mute.
- **Hear yourself while live (headphones only)** plays your own mic back on the phone while
  you're live. It's remembered per phone. Use it with headphones: browser echo cancellation
  is off (it adds delay), so on the speaker it will feed back.
- **Settings → Safety → Max talk** (default 5 minutes) mutes a phone that has been live that
  long, in case one is left live in a pocket. Going live again works straight away.

Under the logo, a dot shows the state of the server and the stream, and next to it the
round trip from this phone to the server and its jitter, measured every 2 seconds over the
same kind of connection the audio uses. Use it to judge a Wi-Fi or VPN link: under 40 ms
with little jitter is good. The dot turns amber when the link is slow (over 100 ms, or
jitter over 33 ms) or a ping went unanswered, when the server missed a health check, or
when the stream isn't running (no destination saved yet, or ffmpeg is failing). Red means
the page can't reach the server. If the server has been updated since the page was opened,
the indicator says so and offers **Reload**.

Settings (the Q-SYS destination and the syslog server) are set on the page and stored in
`~/Pawblic-Address/settings.json`. The service's own log goes to the journal
(`journalctl -u pawblic-address`). Environment variables, set in the service file or before
`python app.py`:

| Variable        | Default        | Purpose                        |
|-----------------|----------------|--------------------------------|
| `PORT`          | `7100`         | HTTPS port for the web page    |
| `FFMPEG_BIN`    | `ffmpeg`       | Path to the ffmpeg binary      |
| `SETTINGS_FILE` | `settings.json`| Where settings are stored      |

## Q-SYS side

Add a **Media Stream Receiver** to the design and set its stream to `rtp://:<port>` (the port
you typed on the page, e.g. `rtp://:4848`). The Core only needs the port; ffmpeg pushes to the
Core's IP. Set the receiver's **Buffer** as low as it will go (50 ms in the Q-SYS versions we've
seen). The server sends a steady stream paced by its own clock, so the receiver doesn't need
headroom for the phone's Wi-Fi.

The stream starts as soon as a destination has been saved and runs until the service stops,
silence included: about 1.4 Mbps for PCM, or the MP3 bitrate. Nothing is sent until a
destination is saved, since the default address is only a placeholder. Saving a new
destination, codec or bitrate restarts ffmpeg with it at once (a gap of about 0.1 s).

Codec options on the page:

| Codec         | What ffmpeg sends                         | Why pick it                                 |
|---------------|-------------------------------------------|---------------------------------------------|
| PCM (default) | L16 stereo 44.1 kHz (static PT 10)        | No encoder delay, lowest latency, ~1.4 Mbps |
| MP3           | Mono MP3, one frame per RTP packet (PT 14)| If bandwidth to the Core is tight           |

Both are things the receiver decodes without an SDP file.

## Where the latency goes

Every place audio can wait, phone to Core:

| Where | Holds | How it's kept short |
|-------|-------|---------------------|
| Phone mic and OS audio stack | 10–40 ms on iPhones, often more on Android | Not under our control. `latencyHint: "interactive"`, no browser DSP |
| AudioWorklet chunking | 10.7 ms (512 samples per message) | `CHUNK_SAMPLES` in `static/app.js` |
| Phone send queue | up to 60 ms, then chunks are dropped | `MAX_BACKLOG_MS` in `static/app.js` |
| Wi-Fi / VPN / TCP | a few ms; a lost packet stalls TCP for 200 ms or more | The round trip shown next to the dot. The server drops what arrives late (next row) |
| Server's TCP ACKs | up to 40 ms if the phone waits for ACKs before sending (Nagle) | `TCP_QUICKACK` after every message |
| Server jitter buffer | 30 ms to start, up to 120 ms after a stall, trimmed back within 1 s | `PREFILL_MS`, `MAX_MS`, `TRIM_EVERY_S`, `TRIM_SLACK_MS` in `stream.py` |
| ffmpeg input | up to 21 ms: it reads raw PCM in 1024-sample packets | Ticks of 512 samples, so they line up with ffmpeg's packets |
| ffmpeg queue and pipe | normally empty; capped at ~85 ms and 4 KB if ffmpeg hiccups | `QUEUE_CHUNKS`, `PIPE_BYTES` in `relay.py` |
| ffmpeg encode + RTP | <1 ms for PCM; ~50–80 ms for MP3 (encoder delay, one frame per packet) | Pick PCM |
| Core's Media Stream Receiver | its **Buffer** setting | Set it to the minimum (see above) |

On the server, a message arriving to its RTP packet leaving takes about 25–35 ms
(`python tests/latency.py` measures it with real ffmpeg).

Why the jitter buffer: before 0.6.0 the server passed the phone's audio straight on, so
every Wi-Fi hiccup reached the Core as a gap followed by a burst. A receiver that re-buffers
after a gap plays everything after it late, and each hiccup can add to the delay. Now the
server sends the Core exactly one tick every 10.7 ms from its own clock, and audio that
arrives late is dropped here: a stall costs a short dropout, never lasting delay. (The
server's and the Core's clocks differ by a few parts per million; if the delay seems to
grow after the stream has run for days, restart the service and tell us.)

Things already done for you, don't undo them:

- The browser sends raw 16-bit PCM from an `AudioWorklet`. MediaRecorder was deliberately avoided; it buffers into container chunks and iOS's output isn't streamable mid-recording.
- Browser echo cancellation, noise suppression and AGC are off (they add delay and colour a PA feed).
- ffmpeg runs with `-probesize 32 -analyzeduration 0`. Without them it buffers about a second of audio before encoding. `-fflags nobuffer` is **not** used: on ffmpeg 6.x it silently breaks raw PCM input.
- RTP packet size is calculated so MP3 goes out one frame per packet instead of ffmpeg packing three frames (~50 ms) into each one.
- If the link stalls, audio is dropped instead of queued, on the phone and in the server's jitter buffer, so the stream stays live rather than drifting behind.
- ffmpeg stays running, so going live doesn't start a process or a new RTP stream.

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

A **page** is one Go live → Mute session. The **stream** is the always-on ffmpeg to the Core.

| Event | Severity | When | Fields |
|-------|----------|------|--------|
| `page_start` | info | Someone goes live | `client`, `codec`, `dest`, `sample_rate`, `ffmpeg_pid` (the stream's), `ua` |
| `page_stop` | info | A page ends, however it ends | `client`, `reason`, `duration_s` (total page time), `audio_s` (audio that reached the server), `dropped_s` (dropped because it arrived too late to play), `gap_s` (silence played because audio hadn't arrived) |
| `page_busy` | warning | Someone tried to go live while another phone was live | `client`, `live_client` |
| `page_rejected` | warning | Refused: another site, a bad or missing start message, an out-of-date page, or no destination saved | `client`, `reason`, `origin` |
| `page_error` | error | Something unexpected failed while handling a phone | `client`, `error` |
| `stream_start` | info | ffmpeg started: at boot, after a settings change, or after a failure | `codec`, `dest`, `ffmpeg_pid` |
| `stream_stop` | info | ffmpeg stopped | `reason` (`settings`, `shutdown`, `error`), `ffmpeg_exit`, `uptime_s` |
| `stream_error` | error | ffmpeg failed or hung; it's restarted after `retry_in_s` (1 s, doubling up to 30 s) | `error`, `retry_in_s` |
| `settings_changed` | info | Settings saved with changes | `client`, then each changed setting as `"old -> new"` |
| `settings_rejected` | warning | Invalid settings, or a save from another site | `client`, `error`, `origin` |
| `console_open` | info | Someone opened the page | `client` |
| `relay_reaped` | error | The watchdog stopped an ffmpeg nobody had fed for 5 s (shouldn't happen; the stream restarts it) | `pid`, `idle_s`, `dest` |
| `syslog_test` | info | **Send syslog test** | `client`, `version` |
| `service_start` / `service_stop` | info | Server starts / stops | `version`, `port`, `https`, `pid` / `version`, `uptime_s` |

`page_stop` reasons: `stopped` (Mute or Release tapped), `left` (page hidden, tab closed or
navigated away), `disconnected` (connection dropped), `timeout` (no audio for 5 s, e.g. the
phone locked or left Wi-Fi), `limit` (live longer than **Max talk**), `error` (something
failed), `shutdown` (server stopped). When `audio_s` is well below `duration_s`, or
`dropped_s` and `gap_s` are more than a fraction of a second, the phone's link was losing
audio; check the round trip on the page from where it was.

Other server log lines at warning or above (ffmpeg errors, for example) are forwarded too,
with MSGID `log`.

## When something goes wrong

There is one ffmpeg, owned by the stream and stopped when the server stops, however it
stops. `tests/soak.py` checks all of these against the real server:

| What happens | What the server does |
|--------------|----------------------|
| Phone taps Mute, hides the page, closes the tab, or drops the connection | Page ends at once; the audio already buffered plays out, then silence |
| Live phone goes quiet (screen locked, off Wi-Fi, no TCP close) | Muted after 5 s without audio, and the line is free again |
| Phone left live (in a pocket) | Muted after **Max talk** (default 5 minutes) |
| Muted phone vanishes | Disconnected after 10 s without its 2-second pings |
| Phone connects but never says hello | Refused after 5 s |
| Wi-Fi stalls, then delivers a burst | Late audio is dropped so the delay doesn't grow; the Core's stream doesn't stop |
| ffmpeg exits (crash) or hangs | Logged as `stream_error`, killed if need be (SIGKILL if it ignores SIGTERM), and restarted after 1 s, backing off to 30 s. A live page carries on |
| The feeder thread gets stuck anyway | A watchdog stops any ffmpeg nobody has fed for 5 s (`relay_reaped`); the stream restarts it |
| Server gets SIGTERM / Ctrl-C | Live page ends (`reason=shutdown`) and ffmpeg is stopped before exit |
| Server is killed outright (SIGKILL, crash) | ffmpeg sees its input close and exits by itself |

Writing audio to ffmpeg never blocks the WebSocket or the stream's clock: it goes through a
short queue, and if ffmpeg falls behind, audio is dropped rather than delayed.

## Security

What's in place:

- The audio and latency WebSockets and the settings API refuse requests from other
  websites (the `Origin` header must match the page). Without this, any web page a staff member opened
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

- **The service won't start, or the install script says it didn't answer**: run `journalctl -u pawblic-address -e` for the error. A port already in use is the usual one.
- **The page loads slowly or not at all, for everyone at once** — fixed in 0.6.1: before that, one phone that opened a connection and went quiet (asleep, out of Wi-Fi, or sitting on the certificate warning) held up every other phone. Update. If it still happens, the server machine or network is the place to look.
- **Red "Can't reach the server"** — the server isn't running, or the phone is on a network that can't reach it.
- **"Server updated… Reload"** — the server was upgraded since the page was opened; tap Reload.
- **No syslog messages** — use **Send syslog test**; check the server's firewall allows UDP out and the syslog server listens on UDP.
- **"This page needs HTTPS"** — you opened `http://`, or `cert.pem`/`key.pem` are missing so the server fell back to plain HTTP.
- **Microphone blocked** — check the site permission in the phone browser; on iOS also Settings → Safari → Microphone.
- **Amber "Save a destination to start the stream"** — nothing is streamed until the Core's address has been saved once.
- **Amber "the stream to the Core isn't running"** — ffmpeg keeps failing; `journalctl -u pawblic-address -e` shows why (`stream_error`).
- **Live, but the Core hears nothing** — confirm the Core IP and that the receiver's port matches. The receiver should show a stream even when nobody is talking.
- **"Another phone is live"** — one phone talks at a time. Go live again once they mute. A phone that silently dropped off frees the line after 5 s.
- **Choppy audio, or `gap_s`/`dropped_s` in `page_stop`** — the phone's link is losing or delaying packets. Watch the round trip next to the dot from the same spot; move closer to the access point, or off the VPN.
- **More delay than expected** — check the Core's receiver Buffer is at its minimum, use PCM, and check the round trip on the page. `python tests/latency.py` shows the server's share.
- **"Muted after N minutes live"** — the **Max talk** limit (Settings → Safety). Go live again, or raise it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # unit and integration tests, about 30 s (Linux, needs ffmpeg for one test)
python tests/soak.py        # soak and leak test against the real server and ffmpeg, about 4 min
python tests/latency.py     # the server's delay, before and after a simulated Wi-Fi stall, 10 s
```

`tests/soak.py` runs hundreds of pages through every way a page can end. It reports the
server's memory, threads, open files and ffmpeg processes, checks the stream runs at real
time as one ffmpeg throughout, and kills the server during a page to check ffmpeg doesn't
outlive it.

## Layout

```
app.py                          Flask routes, the phone WebSocket (talk floor, talk limit), link latency, security
stream.py                       the always-on stream: clock-driven feeder, jitter buffer, ffmpeg restarts
relay.py                        the ffmpeg process (command, writer queue, stop, watchdog)
events.py                       event logging and syslog (RFC 5424 over UDP)
settings.py                     settings.json load/save/validate
version.py                      the version number (bump it with every change)
templates/index.html            the page
static/app.js                   mic, WebSocket, mute/hold-to-talk, meter, sidetone, link latency, tabs, settings
static/pcm-worklet.js           AudioWorklet: Float32 → 48 kHz Int16 PCM chunks while live, and the level
static/logo.svg                 logo (also the favicon)
static/apple-touch-icon.png     home-screen icon for phones
tests/                          pytest suite, fake ffmpegs, soak.py, latency.py
deploy/install-service.sh       installs/updates the systemd service
deploy/pawblic-address.service  systemd unit template
CHANGELOG.md                    what changed in each version
CLAUDE.md                       project rules
```

## Next

- Several saved destinations
- Password on the page (see Security)
- Dockerfile
- Opus over RTP if the Core gains support for it
