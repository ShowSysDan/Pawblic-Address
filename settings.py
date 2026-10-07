"""Load/save settings (settings.json next to this file): the Q-SYS destination and the syslog server."""

import ipaddress
import json
import logging
import os
import re
import threading

log = logging.getLogger("pa.settings")

PATH = os.environ.get(
    "SETTINGS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
)

DEFAULTS = {
    "ip": "192.168.1.100",  # Q-SYS Core IP (or hostname)
    "port": 4848,           # Port set on the Core's Media Stream Receiver (rtp://:4848)
    "codec": "pcm",         # "pcm" (L16 stereo, lowest latency) or "mp3" (mono MP3)
    "bitrate": 128,         # kbps, MP3 only
    "syslog_host": "",      # syslog server IP or hostname; empty turns syslog off
    "syslog_port": 514,     # syslog server UDP port
}

_lock = threading.RLock()
_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


def _host(value, what: str, allow_empty: bool = False) -> str:
    # Only plain IPs and hostnames: the value ends up inside an rtp:// URL for ffmpeg.
    host = str(value).strip()
    if not host and allow_empty:
        return ""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels) \
            or labels[-1].isdigit():  # all-numeric: a mistyped IP, not a hostname
        raise ValueError(f"{what} must be an IP address or hostname")
    return host


def _int(value, what: str, lo: int, hi: int, unit: str = "") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}{unit}")
    return n


def _codec(value) -> str:
    codec = str(value).strip().lower()
    if codec not in ("mp3", "pcm"):
        raise ValueError("Codec must be mp3 or pcm")
    return codec


VALIDATORS = {
    "ip": lambda v: _host(v, "Core IP"),
    "port": lambda v: _int(v, "Port", 1, 65535),
    "codec": _codec,
    "bitrate": lambda v: _int(v, "Bitrate", 32, 320, " kbps"),
    "syslog_host": lambda v: _host(v, "Syslog server", allow_empty=True),
    "syslog_port": lambda v: _int(v, "Syslog port", 1, 65535),
}


def _read() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("%s is unreadable (%s); using defaults", PATH, e)
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict:
    """Stored settings over the defaults. Stored values are re-validated; bad ones fall back."""
    with _lock:
        data = _read()
    cfg = dict(DEFAULTS)
    for key, value in data.items():
        if key in VALIDATORS:
            try:
                cfg[key] = VALIDATORS[key](value)
            except ValueError as e:
                log.warning("ignoring stored %s: %s", key, e)
    return cfg


def save(new: dict) -> tuple:
    """Validate and merge `new` into the stored settings. Raises ValueError on bad input.

    Returns (settings, changes) where changes maps each changed key to (old, new).
    Nothing is written unless every field is valid.
    """
    if not isinstance(new, dict):
        raise ValueError("Expected a JSON object")
    with _lock:
        old = load()
        cfg = dict(old)
        for key in DEFAULTS:
            if key in new:
                cfg[key] = VALIDATORS[key](new[key])

        # Write a temp file and rename it over the old one, so a crash mid-write
        # can't leave a half-written settings.json behind.
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATH)
    return cfg, {k: (old[k], cfg[k]) for k in cfg if old[k] != cfg[k]}
