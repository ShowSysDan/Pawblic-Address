"""Pawblic Address (PA): phone mic -> Flask -> ffmpeg -> Q-SYS Media Stream Receiver.

Run:  python app.py
Open: https://<this-machine-ip>:7100 on the phone (HTTPS is required for mic access).
"""

import atexit
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed

import events
import relay
import settings
from version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("pa.app")

HELLO_TIMEOUT_S = 5            # the phone must say hello this soon after connecting
IDLE_TIMEOUT_S = 5             # no audio for this long ends the page (phone locked, Wi-Fi gone)
POLL_S = 0.5                   # how often the page loop wakes up when no audio is arriving
CLOSE_GRACE_S = 1.0            # how long a phone gets to acknowledge a WebSocket close
MAX_MESSAGE_BYTES = 64 * 1024  # one 20 ms audio chunk is ~2 KB
SAMPLE_RATES = range(8000, 192001)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["SOCK_SERVER_OPTIONS"] = {"max_message_size": MAX_MESSAGE_BYTES}
sock = Sock(app)

# One live page at a time.
_stream_lock = threading.Lock()
_page = None        # the live page, see _Page
_page_lock = threading.Lock()
_started = time.monotonic()
_shutting_down = False


class _Refused(Exception):
    pass


class _Page:
    """One go-live session, for the page_start/page_stop events."""

    def __init__(self, client: str, sample_rate: int, relay_: relay.FFmpegRelay):
        self.client = client
        self.sample_rate = sample_rate
        self.relay = relay_
        self.started = time.monotonic()
        self.received = 0
        self.ended = False

    def end(self, reason: str):
        with _page_lock:
            if self.ended:
                return
            self.ended = True
        per_s = 2 * self.sample_rate  # 16-bit mono
        events.emit("page_stop", client=self.client, reason=reason,
                    duration_s=round(time.monotonic() - self.started, 1),
                    audio_s=round(self.received / per_s, 1),
                    dropped_s=round(self.relay.dropped_bytes / per_s, 1),
                    ffmpeg_exit=self.relay.returncode)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _same_origin() -> bool:
    """Browsers send Origin on WebSockets and cross-site POSTs; refuse other sites' pages.

    Without this, any web page a staff member opens could stream audio onto the PA or
    change the destination, using their browser on the venue network.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # not a browser (curl, scripts), so not driven by another site
    return urlsplit(origin).netloc.lower() == request.host.lower()


@app.get("/")
def index():
    events.emit("console_open", client=request.remote_addr)
    return render_template("index.html", version=VERSION)


@app.get("/api/health")
def health():
    return jsonify(ok=True, version=VERSION, live=_page is not None)


@app.get("/api/settings")
def get_settings():
    return jsonify(settings.load())


def _shown(value) -> str:
    return "none" if value == "" else str(value)


@app.post("/api/settings")
def save_settings():
    client = request.remote_addr
    if not _same_origin():
        events.emit("settings_rejected", level=logging.WARNING, client=client,
                    error="request from another site", origin=request.headers.get("Origin"))
        return jsonify(error="Refused: request came from another site"), 403
    if not request.is_json:
        return jsonify(error="Send settings as application/json"), 415
    try:
        cfg, changes = settings.save(request.get_json(silent=True))
    except ValueError as e:
        events.emit("settings_rejected", level=logging.WARNING, client=client, error=str(e))
        return jsonify(error=str(e)), 400
    if changes:
        # Logged before syslog is re-pointed, so the old server sees it was moved.
        events.emit("settings_changed", client=client,
                    **{k: f"{_shown(old)} -> {_shown(new)}" for k, (old, new) in changes.items()})
    events.configure(cfg["syslog_host"], cfg["syslog_port"])
    return jsonify(cfg)


@app.post("/api/syslog/test")
def syslog_test():
    if not _same_origin():
        return jsonify(error="Refused: request came from another site"), 403
    target = events.active_target()
    if target is None:
        return jsonify(error="Syslog is off, or its server name didn't resolve. "
                             "Check the server and save."), 400
    events.emit("syslog_test", client=request.remote_addr, version=VERSION)
    return jsonify(sent_to=target)


def _send(ws, **msg):
    try:
        ws.send(json.dumps(msg))
    except Exception:
        pass


def _close(ws):
    """Close the WebSocket and make sure its reader thread and socket are gone.

    simple-websocket's reader thread sits in recv() until the phone answers the close.
    A phone that dropped off the network never answers, which would leak a thread and
    a socket per lost phone, so after a grace period the socket is shut down regardless.
    Shutting it down also stops Werkzeug's dev server writing a stray HTTP response
    after the last frame, which browsers report as "Invalid frame header".
    """
    try:
        ws.close()
    except Exception:
        pass
    ws.thread.join(CLOSE_GRACE_S)
    try:
        ws.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    ws.thread.join(CLOSE_GRACE_S)


def _hello(ws) -> int:
    """First message: JSON {"sampleRate": N}. Returns the sample rate."""
    msg = ws.receive(timeout=HELLO_TIMEOUT_S)
    if msg is None:
        raise _Refused("The phone didn't start the stream in time")
    try:
        rate = int(json.loads(msg).get("sampleRate", 48000))
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise _Refused("The phone sent a bad start message")
    if rate not in SAMPLE_RATES:
        raise _Refused(f"Unsupported sample rate {rate}")
    return rate


@sock.route("/ws/audio")
def audio(ws):
    """First message: JSON {"sampleRate": N}. Every message after: raw s16le mono PCM."""
    try:
        _run_page(ws, request.remote_addr, request.headers.get("User-Agent", ""))
    finally:
        _close(ws)


def _run_page(ws, client: str, user_agent: str):
    global _page
    if not _same_origin():
        events.emit("page_rejected", level=logging.WARNING, client=client,
                    reason="request from another site", origin=request.headers.get("Origin"))
        _send(ws, type="error", msg="Refused: this page was opened from another site.")
        return
    if not _stream_lock.acquire(blocking=False):
        live = _page
        events.emit("page_busy", level=logging.WARNING, client=client,
                    live_client=live.client if live else None)
        _send(ws, type="error", msg="Another phone is already live. Stop it first.")
        return

    r = page = None
    reason = "error"
    try:
        sample_rate = _hello(ws)
        cfg = settings.load()
        r = relay.FFmpegRelay(cfg, sample_rate).start()
        page = _page = _Page(client, sample_rate, r)
        events.emit("page_start", client=client, codec=cfg["codec"],
                    dest=f"{cfg['ip']}:{cfg['port']}", sample_rate=sample_rate,
                    ffmpeg_pid=r.pid, ua=user_agent[:160])
        _send(ws, type="live", msg=f"{cfg['codec'].upper()} to {cfg['ip']}:{cfg['port']}")

        last_audio = time.monotonic()
        while True:
            data = ws.receive(timeout=POLL_S)
            if data is None:
                if not ws.connected:
                    # simple-websocket can drop a connection (e.g. message too big)
                    # without waking receive(), so check rather than wait it out.
                    raise ConnectionClosed(ws.close_reason, ws.close_message)
                if time.monotonic() - last_audio >= IDLE_TIMEOUT_S:
                    reason = "timeout"
                    _send(ws, type="error", msg=f"No audio reached the server for "
                                                f"{IDLE_TIMEOUT_S} s, so the page ended.")
                    break
                continue
            if isinstance(data, bytes):
                last_audio = time.monotonic()
                page.received += len(data)
                r.write(data)
    except ConnectionClosed as e:
        # The page sends 1000 when Stop is tapped and browsers send 1001 when the tab goes
        # away; anything else (including no close at all) is a dropped connection.
        reason = {1000: "stopped", 1001: "left"}.get(e.reason, "disconnected")
    except _Refused as e:
        events.emit("page_rejected", level=logging.WARNING, client=client, reason=str(e))
        _send(ws, type="error", msg=str(e))
    except relay.RelayError as e:
        if not _shutting_down:  # at shutdown the relay is stopped under us; not an error
            events.emit("page_error", level=logging.ERROR, client=client, error=str(e))
        _send(ws, type="error", msg=str(e))
    except Exception as e:
        log.exception("page from %s failed", client)
        events.emit("page_error", level=logging.ERROR, client=client, error=repr(e))
        _send(ws, type="error", msg="Server error; see the server log.")
    finally:
        if r:
            r.stop()
        _page = None
        _stream_lock.release()
        if page:
            page.end(reason)


def _on_exit():
    global _shutting_down
    _shutting_down = True
    page = _page
    if page:
        page.end("shutdown")
    relay.stop_all()
    events.emit("service_stop", version=VERSION,
                uptime_s=round(time.monotonic() - _started, 1))


class _NoHealthChecks(logging.Filter):
    # The page polls /api/health every few seconds; keep it out of the access log.
    def filter(self, record):
        return "/api/health" not in record.getMessage()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cert, key = os.path.join(here, "cert.pem"), os.path.join(here, "key.pem")
    ssl_context = (cert, key) if os.path.exists(cert) and os.path.exists(key) else None
    if ssl_context is None:
        log.warning("cert.pem/key.pem not found: serving plain HTTP. Phones will NOT "
                    "allow mic access over http. See README for a one-line cert.")
    port = int(os.environ.get("PORT", 7100))

    cfg = settings.load()
    events.configure(cfg["syslog_host"], cfg["syslog_port"])
    logging.getLogger("werkzeug").addFilter(_NoHealthChecks())

    # systemd/docker stop with SIGTERM: exit normally so _on_exit stops ffmpeg.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    atexit.register(_on_exit)
    events.emit("service_start", version=VERSION, port=port,
                https=ssl_context is not None, pid=os.getpid())

    app.run(host="0.0.0.0", port=port, ssl_context=ssl_context, threaded=True, debug=False)


if __name__ == "__main__":
    main()
