"""Pawblic Address (PA): phone mic -> Flask -> ffmpeg -> Q-SYS Media Stream Receiver.

Run:  python app.py
Open: https://<this-machine-ip>:5000 on the phone (HTTPS is required for mic access).
"""

import json
import logging
import os
import threading

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock

import relay
import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)
sock = Sock(app)

# One live stream at a time for the proof of concept.
_stream_lock = threading.Lock()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/settings")
def get_settings():
    return jsonify(settings.load())


@app.post("/api/settings")
def save_settings():
    try:
        cfg = settings.save(request.get_json(force=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("settings saved: %s", cfg)
    return jsonify(cfg)


def _send(ws, **msg):
    try:
        ws.send(json.dumps(msg))
    except Exception:
        pass


@sock.route("/ws/audio")
def audio(ws):
    """First message: JSON {"sampleRate": N}. Every message after: raw s16le mono PCM."""
    if not _stream_lock.acquire(blocking=False):
        _send(ws, type="error", msg="Another phone is already live. Stop it first.")
        return

    r = None
    try:
        hello = json.loads(ws.receive())
        sample_rate = int(hello.get("sampleRate", 48000))
        cfg = settings.load()

        r = relay.FFmpegRelay(cfg, sample_rate).start()
        _send(ws, type="live", msg=f"{cfg['codec'].upper()} to {cfg['ip']}:{cfg['port']}")
        log.info("live: %s (%s, %d Hz)", r.destination, cfg["codec"], sample_rate)

        while True:
            data = ws.receive()
            if isinstance(data, bytes):
                r.write(data)
    except relay.RelayError as e:
        log.error("%s", e)
        _send(ws, type="error", msg=str(e))
    finally:
        if r:
            r.stop()
        _stream_lock.release()
        log.info("session ended")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    cert, key = os.path.join(here, "cert.pem"), os.path.join(here, "key.pem")
    ssl_context = (cert, key) if os.path.exists(cert) and os.path.exists(key) else None
    if ssl_context is None:
        log.warning("cert.pem/key.pem not found: serving plain HTTP. Phones will NOT "
                    "allow mic access over http. See README for a one-line cert.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            ssl_context=ssl_context, threaded=True, debug=False)
