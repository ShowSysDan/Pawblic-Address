# Changelog

Every change to Pawblic Address gets a new version number here and in `version.py`
(see CLAUDE.md). Newest first.

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
