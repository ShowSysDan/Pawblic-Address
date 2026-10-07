"""Load/save the relay destination settings (settings.json next to this file)."""

import json
import os
import threading

PATH = os.environ.get(
    "SETTINGS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
)

DEFAULTS = {
    "ip": "192.168.1.100",  # Q-SYS Core IP (or hostname)
    "port": 4848,           # Port set on the Core's Media Stream Receiver (rtp://:4848)
    "codec": "pcm",         # "pcm" (L16 stereo, lowest latency) or "mp3" (mono MP3)
    "bitrate": 128,         # kbps, MP3 only
}

_lock = threading.Lock()


def load() -> dict:
    with _lock:
        try:
            with open(PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(new: dict) -> dict:
    """Validate and merge `new` into the stored settings. Raises ValueError on bad input."""
    cfg = load()

    if "ip" in new:
        ip = str(new["ip"]).strip()
        if not ip or any(c in ip for c in " /?#@"):
            raise ValueError("IP address or hostname looks wrong")
        cfg["ip"] = ip

    if "port" in new:
        try:
            port = int(new["port"])
        except (TypeError, ValueError):
            raise ValueError("Port must be a number")
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        cfg["port"] = port

    if "codec" in new:
        codec = str(new["codec"]).lower()
        if codec not in ("mp3", "pcm"):
            raise ValueError("Codec must be mp3 or pcm")
        cfg["codec"] = codec

    if "bitrate" in new:
        try:
            bitrate = int(new["bitrate"])
        except (TypeError, ValueError):
            raise ValueError("Bitrate must be a number")
        if not 32 <= bitrate <= 320:
            raise ValueError("Bitrate must be between 32 and 320 kbps")
        cfg["bitrate"] = bitrate

    with _lock:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg
