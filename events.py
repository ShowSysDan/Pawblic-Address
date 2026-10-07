"""Operational events (pages, settings changes, errors) -> the local log and, if set, syslog.

    events.emit("page_stop", client="10.0.0.5", duration_s=12.3)

logs  `event=page_stop client=10.0.0.5 duration_s=12.3`  and, when a syslog server is
configured, sends it as RFC 5424 over UDP with MSGID `page_stop`. Other Pawblic Address
log lines at WARNING and above (ffmpeg errors and so on) are forwarded too, MSGID `log`.

The event list is in the README; keep it in step when adding or changing events.
"""

import datetime
import logging
import logging.handlers
import os
import re
import socket
import threading

APP_NAME = "pawblic-address"
FACILITY = logging.handlers.SysLogHandler.LOG_LOCAL0
MAX_MESSAGE = 1500  # keep each datagram well under the 2048 bytes receivers must accept

log = logging.getLogger("pa.events")
_pa = logging.getLogger("pa")
_pa.setLevel(logging.INFO)
_local = logging.getLogger("syslog-handler")  # not under "pa", so its lines never loop back

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NEEDS_QUOTES = re.compile(r'[\s"=\\]')
_HOSTNAME = re.sub(r"[^!-~]", "", socket.gethostname())[:255] or "-"

_lock = threading.Lock()
_handler = None
_target = None  # (host, port) as configured, or None when syslog is off


def _value(v) -> str:
    if isinstance(v, float):
        v = f"{v:.1f}"
    # No control characters, so one event is always exactly one log line.
    s = _CONTROL.sub(" ", str(v))[:200]
    if not s or _NEEDS_QUOTES.search(s):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def format_event(name: str, fields: dict) -> str:
    parts = [f"event={name}"]
    parts += [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]
    return " ".join(parts)


def emit(name: str, level: int = logging.INFO, **fields) -> None:
    log.log(level, "%s", format_event(name, fields), extra={"event": name})


class _RFC5424(logging.Formatter):
    def format(self, record):
        ts = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        stamp = f"{ts:%Y-%m-%dT%H:%M:%S}.{ts.microsecond // 1000:03d}Z"
        msgid = getattr(record, "event", None) or "log"
        msg = record.getMessage()
        if msgid == "log":
            msg = f"{record.name} {record.levelname}: {msg}"
        msg = _CONTROL.sub(" ", msg)[:MAX_MESSAGE]
        return f"1 {stamp} {_HOSTNAME} {APP_NAME} {os.getpid()} {msgid} - {msg}"


def _wanted(record) -> bool:
    return record.name == log.name or record.levelno >= logging.WARNING


class _SyslogUDP(logging.handlers.SysLogHandler):
    append_nul = False  # RFC 5424 framing; the NUL is an old rsyslog-ism
    failures = 0

    def handleError(self, record):
        # Server unreachable or the network is down: never raise into the caller,
        # and don't print a traceback for every event.
        self.failures += 1
        if self.failures in (1, 10, 100) or self.failures % 1000 == 0:
            _local.warning("syslog: send to %s failed (%d so far)", self.address, self.failures)


def configure(host: str, port: int) -> None:
    """Send events to host:port over UDP. An empty host turns syslog off."""
    global _handler, _target
    target = (host, int(port)) if host else None
    with _lock:
        if target == _target and (_handler is not None or target is None):
            return
        old, _handler, _target = _handler, None, target
        if old is not None:
            _pa.removeHandler(old)
            old.close()
        if target is None:
            log.info("syslog off")
            return
        # Resolve once here: SysLogHandler would otherwise look the name up on every send.
        try:
            addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0][4]
        except OSError as e:
            log.warning("syslog: can't resolve %s (%s); syslog stays off until settings are "
                        "saved again", host, e)
            return
        handler = _SyslogUDP(address=addr[:2], facility=FACILITY, socktype=socket.SOCK_DGRAM)
        handler.setFormatter(_RFC5424())
        handler.addFilter(_wanted)
        _pa.addHandler(handler)
        _handler = handler
    log.info("syslog -> %s:%s (UDP)", host, port)


def active_target():
    """Return "host:port" while syslog is on and its server resolved, else None."""
    with _lock:
        return f"{_target[0]}:{_target[1]}" if _handler is not None else None
