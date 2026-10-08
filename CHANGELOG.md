# Changelog

Every change to Pawblic Address gets a new version number here and in `version.py`
(see CLAUDE.md). Newest first.

## [0.6.0] - 2026-10-08

### Changed
- The stream to the Core is always on: one ffmpeg, started when the server starts (once a
  destination is saved) and fed from the server's clock, with silence when nobody talks.
  Going live no longer starts ffmpeg or a new RTP stream for the Core to lock on to.
- The button is now Go live / Mute. The first tap opens the mic and connects; after that,
  muting and going live again are instant. The mic is released when the page is hidden or
  **Release the mic** is tapped. Other phones can stay connected while one talks.
- Lower and steadier delay: a server-side jitter buffer (30 ms, trimmed back within 1 s
  after a Wi-Fi stall) means the Core gets an even stream and late audio is dropped instead
  of piling up as delay. Chunks from the phone are 10.7 ms (were 20 ms). The ffmpeg queue
  and pipe are capped so they can't hold a backlog. The server sends TCP ACKs at once.
- Saving a new destination, codec or bitrate applies at once (ffmpeg restarts).
- ffmpeg that dies or hangs is restarted with backoff, and a live page carries on.
- Phones always send 48 kHz; the worklet resamples if a browser won't run at 48 kHz.
- `page_stop` has `gap_s` (silence played because audio was late) and no longer
  `ffmpeg_exit`; `dropped_s` now counts audio dropped for arriving too late.
  `/api/health` reports the stream's state. The watchdog acts after 5 s (was 15 s).

- The page has **Talk** and **Settings** tabs (the tab is remembered per phone).
- "Saved." under the settings fades after a few seconds, so each save visibly confirms.

### Added
- The round trip from the phone to the server, and its jitter, shown next to the
  connection dot and updated every 2 s, mic or not (`/ws/ping`). A slow or lossy link turns
  the dot amber.
- Level meter under the button, live or muted.
- **Hold to talk** (per phone): hold the button, or the space bar, to talk.
- **Max talk** in Settings → Safety (default 5 minutes): a phone left live is muted.
  `page_stop` reason `limit`.
- **Hear yourself while live** (sidetone) for talkers with headphones.
- Events `stream_start`, `stream_stop`, `stream_error`.
- `tests/latency.py`: measures the server's delay, before and after a simulated stall.

## [0.5.0] - 2026-10-07

### Added
- Runs as a systemd service from the user's home folder. `deploy/install-service.sh` sets up
  the venv and certificate, installs and starts the service, checks it answers, and doubles
  as the updater. The unit template is `deploy/pawblic-address.service`.
- README install guide: packages, install, firewall, managing the service, updating,
  uninstalling, installing by hand.

## [0.4.0] - 2026-10-07

### Added
- Version number in the page footer and in syslog (`service_start`, `syslog_test`).
- Server connection indicator on the page, via a new `/api/health`. It says when the server
  was updated since the page was opened and offers Reload.
- Syslog: server and port settings on the page, a **Send syslog test** button, and RFC 5424
  events over UDP: page start/stop with total page time, settings changes, refused and busy
  attempts, errors, page opens, service start/stop. Listed in the README.
- Tests: pytest suite (`tests/`) and a soak/leak test against the real server
  (`tests/soak.py`).
- Project rules in CLAUDE.md.

### Changed
- ffmpeg handling rebuilt so a glitch can't leave ffmpeg running or the line stuck:
  - audio goes through a short non-blocking queue;
  - a hung ffmpeg is detected and killed;
  - stopping always finishes in bounded time, escalating to SIGKILL;
  - a watchdog stops any ffmpeg nobody is feeding;
  - SIGTERM shuts down cleanly.
- A page now ends after 5 s without audio (phone locked or off Wi-Fi), and a phone that
  connects but never starts streaming is dropped after 5 s. Before, both held the line
  forever.
- WebSocket connections are always fully closed, which also removes the browser's "Invalid
  frame header" console error at Stop.
- The page tells the server when Stop was tapped (close code 1000), so logs can tell a Stop
  from a dropped connection.

### Security
- The audio WebSocket and the settings API refuse requests from other websites (cross-site
  WebSocket hijacking and CSRF).
- Settings must be JSON; bodies are capped at 16 KB and WebSocket messages at 64 KB.
- Stricter validation of Core and syslog addresses. Stored settings are re-validated on
  load, and settings.json is written atomically.
- Content-Security-Policy, X-Frame-Options, nosniff and Referrer-Policy headers.
- Log values are sanitised so clients can't forge log lines.

## [0.3.0] - 2026-10-07

### Changed
- The web page's default port is now 7100 (was 5000). `PORT` still overrides it.

## [0.2.0] - 2026-10-07

### Added
- Pawblic Address (PA) branding: logo, favicon, home-screen icon, page header and README.

## [0.1.0] - 2026-10-07

### Added
- The qsys-mic-relay starter: phone mic → Flask WebSocket → ffmpeg → RTP to a Q-SYS
  Media Stream Receiver.
