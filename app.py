"""Pawblic Address (PA): phone mic -> Flask -> ffmpeg -> Q-SYS Media Stream Receiver.

The stream to the Core runs all the time (stream.py). Phones connect once, keep the mic
open while the page is showing, and the button just takes or gives up the talk floor.

Run:  python app.py
Open: https://<this-machine-ip>:7100 on the phone (HTTPS is required for mic access).
"""

import atexit
import json
import logging
import math
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.serving import ThreadedWSGIServer

import events
import relay
import settings
import stream
from version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("pa.app")

HELLO_TIMEOUT_S = 5            # the phone must say hello this soon after connecting
IDLE_TIMEOUT_S = 5             # live with no audio for this long: the phone is muted (locked, Wi-Fi gone)
SILENT_TIMEOUT_S = 10          # nothing at all from a phone for this long: it's gone (it pings every 2 s)
POLL_S = 0.5                   # how often a connection wakes up when nothing is arriving
CLOSE_GRACE_S = 1.0            # how long a phone gets to acknowledge a WebSocket close
HANDSHAKE_TIMEOUT_S = 10       # a connection gets this long to finish its TLS handshake
TLS_HANDSHAKE = 0x16           # first byte of every TLS connection; anything else is plain HTTP
MAX_MESSAGE_BYTES = 64 * 1024  # one 10.7 ms audio chunk is 1 KB
STREAM_KEYS = ("ip", "port", "codec", "bitrate")  # settings the stream is built from

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["SOCK_SERVER_OPTIONS"] = {"max_message_size": MAX_MESSAGE_BYTES}
sock = Sock(app)

_stream = stream.Stream()

# One phone talks at a time: whoever holds the floor.
_floor_lock = threading.Lock()
_page = None        # the live page, see _Page
_started = time.monotonic()


class _Refused(Exception):
    pass


class _Page:
    """One talk: from Go live to Mute (or however it ends). Holds the floor while it lasts."""

    def __init__(self, client: str, limit_s: float):
        self.client = client
        self.limit_s = limit_s  # 0: no limit
        self.started = time.monotonic()
        self.received = 0
        self.ended = False
        self.token = _stream.buffer.open()

    def push(self, data: bytes):
        self.received += len(data)
        _stream.buffer.push(self.token, data)

    def end(self, reason: str):
        global _page
        with _floor_lock:
            if self.ended:
                return
            self.ended = True
            if _page is self:
                _page = None
            dropped, gaps = _stream.buffer.close(self.token)
        per_s = 2 * stream.RATE  # 16-bit mono
        events.emit("page_stop", client=self.client, reason=reason,
                    duration_s=round(time.monotonic() - self.started, 1),
                    audio_s=round(self.received / per_s, 1),
                    dropped_s=round(dropped / per_s, 1),
                    gap_s=round(gaps / per_s, 1))


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
    return jsonify(ok=True, version=VERSION, live=_page is not None, stream=_stream.state())


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
    if any(k in changes for k in STREAM_KEYS) or _stream.state() != "up":
        _stream.restart()  # returns at once; the feeder thread swaps ffmpeg over
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


def _hello(ws):
    """First message: JSON {"type": "hello", "sampleRate": 48000}."""
    msg = ws.receive(timeout=HELLO_TIMEOUT_S)
    if msg is None:
        raise _Refused("The phone didn't start the stream in time")
    try:
        hello = json.loads(msg)
        kind, rate = hello.get("type"), hello.get("sampleRate")
    except (ValueError, TypeError, AttributeError):
        raise _Refused("The phone sent a bad start message")
    if kind != "hello":
        # Pages from before 0.6.0 sent {"sampleRate": N} and expected to be live at once.
        raise _Refused("This page is out of date. Reload it.")
    if rate != stream.RATE:
        raise _Refused(f"Unsupported sample rate {rate}")


def _dest() -> str:
    cfg = _stream.cfg or settings.load()
    return f"{cfg['codec'].upper()} to {cfg['ip']}:{cfg['port']}"


def _quickack(ws):
    """Ask Linux to acknowledge what the phone sent straight away.

    Linux delays ACKs by up to 40 ms. A phone whose TCP stack holds small writes until the
    last one is acknowledged (Nagle's algorithm) would then send audio in lumps. Setting
    TCP_QUICKACK sends any pending ACK now; it wears off, so it's set after every message.
    """
    try:
        ws.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
    except (AttributeError, OSError):
        pass


def _pong(ws, msg: dict):
    t = msg.get("t")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
        _send(ws, type="pong", t=t)


@sock.route("/ws/ping")
def ping(ws):
    """Link latency: echoes {"type": "ping", "t": T} as {"type": "pong", "t": T}.

    The page keeps this open while it's showing and pings every 2 s, mic or not, to show
    the round trip to the server (Wi-Fi, VPN) next to the connection dot. It's the same
    kind of connection as the audio, so it sees the same delays. Ends when the pings stop.
    """
    try:
        if not _same_origin():
            return
        while True:
            msg = ws.receive(timeout=SILENT_TIMEOUT_S)
            if msg is None:
                return
            _quickack(ws)
            try:
                _pong(ws, json.loads(msg))
            except (ValueError, AttributeError):
                return
    finally:
        _close(ws)


@sock.route("/ws/audio")
def audio(ws):
    """See _run_phone for the protocol."""
    try:
        _run_phone(ws, request.remote_addr, request.headers.get("User-Agent", ""))
    finally:
        _close(ws)


def _take_floor(ws, client: str, user_agent: str):
    """Make this phone the talker, or tell it why not. Returns the _Page or None."""
    global _page
    if not settings.is_configured():
        events.emit("page_rejected", level=logging.WARNING, client=client,
                    reason="no destination saved")
        _send(ws, type="refused", msg="Set the Core's IP under Destination and save first.")
        return None
    limit_s = settings.load()["max_talk_min"] * 60
    with _floor_lock:
        live = _page
        if live is None:
            page = _page = _Page(client, limit_s)
    if live is not None:
        events.emit("page_busy", level=logging.WARNING, client=client, live_client=live.client)
        _send(ws, type="refused", msg="Another phone is live. Try again when it mutes.")
        return None
    cfg = _stream.cfg or settings.load()
    events.emit("page_start", client=client, codec=cfg["codec"],
                dest=f"{cfg['ip']}:{cfg['port']}", sample_rate=stream.RATE,
                ffmpeg_pid=_stream.pid, ua=user_agent[:160])
    _send(ws, type="live", msg=_dest())
    return page


def _run_phone(ws, client: str, user_agent: str):
    """One phone, from opening the mic to leaving the page.

    Phone -> server: {"type": "hello", "sampleRate": 48000} first, then any of
      {"type": "talk"}  take the floor; audio sent after it is played if the floor was free
      {"type": "mute"}  give the floor up
      {"type": "ping", "t": T}  every 2 s, so a phone that vanished can be told from a
                        quiet one; answered with {"type": "pong", "t": T} to time the round trip
      binary            48 kHz s16le mono PCM, only played while this phone holds the floor
    Server -> phone: {"type": "ready" | "live" | "muted" | "refused" | "error", "msg": ...}
    and pongs.
    """
    if not _same_origin():
        events.emit("page_rejected", level=logging.WARNING, client=client,
                    reason="request from another site", origin=request.headers.get("Origin"))
        _send(ws, type="error", msg="Refused: this page was opened from another site.")
        return

    page = None
    reason = "error"
    try:
        _hello(ws)
        _send(ws, type="ready", msg=_dest())
        last_heard = last_audio = time.monotonic()
        while True:
            data = ws.receive(timeout=POLL_S)
            now = time.monotonic()
            if page and page.limit_s and now - page.started >= page.limit_s:
                # A phone left live (in a pocket, say): mute it. It can go live again.
                minutes = round(page.limit_s / 60)
                page.end("limit")
                page = None
                _send(ws, type="muted", msg=f"Muted after {minutes} minute{'s' * (minutes != 1)} "
                                            f"live, the limit set in Settings. Go live to carry on.")
            if data is None:
                if not ws.connected:
                    # simple-websocket can drop a connection (e.g. message too big)
                    # without waking receive(), so check rather than wait it out.
                    raise ConnectionClosed(ws.close_reason, ws.close_message)
                if now - last_heard >= SILENT_TIMEOUT_S:
                    reason = "timeout"
                    _send(ws, type="error", msg="Lost contact with this phone.")
                    break
                if page and now - last_audio >= IDLE_TIMEOUT_S:
                    page.end("timeout")
                    page = None
                    _send(ws, type="muted", msg=f"No audio reached the server for "
                                                f"{IDLE_TIMEOUT_S} s, so you were muted.")
                continue
            last_heard = now
            if isinstance(data, bytes):
                _quickack(ws)
                if page:
                    last_audio = now
                    page.push(data)
                continue  # muted, or refused: audio sent optimistically is dropped
            try:
                msg = json.loads(data)
                kind = msg.get("type")
            except (ValueError, AttributeError):
                msg, kind = {}, None
            if kind == "ping":
                _pong(ws, msg)
            elif kind == "talk" and not page:
                page = _take_floor(ws, client, user_agent)
                last_audio = now
            elif kind == "mute" and page:
                page.end("stopped")
                page = None
                _send(ws, type="muted")
    except ConnectionClosed as e:
        # The page sends 1000 when Release is tapped and browsers send 1001 when the tab
        # goes away; anything else (including no close at all) is a dropped connection.
        reason = {1000: "stopped", 1001: "left"}.get(e.reason, "disconnected")
    except _Refused as e:
        events.emit("page_rejected", level=logging.WARNING, client=client, reason=str(e))
        _send(ws, type="error", msg=str(e))
    except Exception as e:
        log.exception("phone %s failed", client)
        events.emit("page_error", level=logging.ERROR, client=client, error=repr(e))
        _send(ws, type="error", msg="Server error; see the server log.")
    finally:
        if page:
            page.end(reason)


def _on_exit():
    page = _page
    if page:
        page.end("shutdown")
    _stream.stop()
    relay.stop_all()  # belt and braces: the feeder stops its ffmpeg in a finally
    events.emit("service_stop", version=VERSION,
                uptime_s=round(time.monotonic() - _started, 1))


class _NoHealthChecks(logging.Filter):
    # The page polls /api/health every few seconds; keep it out of the access log.
    def filter(self, record):
        return "/api/health" not in record.getMessage()


class _Server(ThreadedWSGIServer):
    """Werkzeug's threaded server, with each TLS handshake done in its connection's thread.

    Werkzeug wraps the listening socket in TLS, so accept() does the handshake on the one
    thread that accepts every connection, with no time limit. A single phone that opens a
    connection and then goes quiet (asleep, out of Wi-Fi, sitting on the certificate
    warning) stops the page loading for everyone until it goes away. Here the listening
    socket stays plain and the handshake runs in the connection's own thread, with a limit.
    That also lets plain http:// on the same port be redirected to https://.
    """

    def __init__(self, host: str, port: int, wsgi_app, ssl_context: ssl.SSLContext | None):
        super().__init__(host, port, wsgi_app)
        self.ssl_context = ssl_context  # Werkzeug reads it for the https:// scheme

    def finish_request(self, request, client_address):
        if self.ssl_context is None:
            return super().finish_request(request, client_address)
        try:
            request.settimeout(HANDSHAKE_TIMEOUT_S)
            first = request.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first[0] != TLS_HANDSHAKE:
                _redirect_to_https(request)
                return
            conn = self.ssl_context.wrap_socket(request, server_side=True)
        except (OSError, ValueError):
            return  # the certificate was refused, or it went quiet: drop it
        try:
            conn.settimeout(None)
            super().finish_request(conn, client_address)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


_HOST = re.compile(r"[A-Za-z0-9.\-]+(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?")
_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?\-]*")


def _redirect_to_https(sock: socket.socket):
    """Someone typed the address without https://: send them to the same URL over HTTPS.

    Only the Host header and path from the request are used, and both are checked, so
    the reply can't be steered anywhere but this server's own address.
    """
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 8192:
        chunk = sock.recv(4096)
        if not chunk:
            return
        head += chunk
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1").split(" ")
    path = parts[1] if len(parts) == 3 and _PATH.fullmatch(parts[1]) else "/"
    host = ""
    for line in lines[1:]:
        name, _, value = line.decode("latin-1").partition(":")
        if name.strip().lower() == "host":
            host = value.strip()
    if not _HOST.fullmatch(host):
        ip, port = sock.getsockname()[:2]
        host = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    body = b"This page needs HTTPS.\n"
    sock.sendall(f"HTTP/1.1 307 Temporary Redirect\r\n"
                 f"Location: https://{host}{path}\r\n"
                 f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                 f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)


def _tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cert, key = os.path.join(here, "cert.pem"), os.path.join(here, "key.pem")
    ssl_context = (_tls_context(cert, key)
                   if os.path.exists(cert) and os.path.exists(key) else None)
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
    _stream.start()
    if not settings.is_configured():
        log.warning("no destination saved yet: the stream to the Core starts once one is")

    server = _Server("0.0.0.0", port, app, ssl_context)
    log.info("serving on %s://0.0.0.0:%d", "https" if ssl_context else "http", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
