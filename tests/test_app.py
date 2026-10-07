import json
import os
import re
import socket
import threading
import time

import pytest
import simple_websocket
from werkzeug.serving import make_server

import app as pa
import events
import relay
from conftest import SyslogSink, child_pids, fake_ffmpeg
from version import VERSION

CHUNK = b"\0\0" * 960  # 20 ms of 48 kHz mono s16le


# ---- HTTP -------------------------------------------------------------------

@pytest.fixture
def client():
    return pa.app.test_client()


def test_page_shows_the_version(client):
    html = client.get("/").get_data(as_text=True)
    assert f"Pawblic Address v{VERSION}" in html
    assert f'data-version="{VERSION}"' in html
    assert f"/static/app.js?v={VERSION}" in html


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_health(client):
    r = client.get("/api/health")
    assert r.get_json() == {"ok": True, "version": VERSION, "live": False}
    assert r.headers["Cache-Control"] == "no-store"


def test_console_open_event_is_rfc5424(client, syslog):
    client.get("/")
    msg = syslog.wait_for("console_open")
    # <local0.info>1 TIMESTAMP HOST APP PID MSGID - MSG
    assert re.fullmatch(r"<134>1 \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \S+ pawblic-address "
                        r"\d+ console_open - event=console_open client=127\.0\.0\.1", msg), msg


def test_settings_change_is_logged(client, syslog):
    r = client.post("/api/settings", json={"port": 5000, "codec": "mp3"})
    assert r.status_code == 200 and r.get_json()["port"] == 5000
    msg = syslog.wait_for("settings_changed")
    assert 'port="4848 -> 5000"' in msg and 'codec="pcm -> mp3"' in msg


def test_settings_must_be_json(client):
    # A cross-site <form> can only send form or text/plain bodies.
    r = client.post("/api/settings", data='{"port": 5000}', content_type="text/plain")
    assert r.status_code == 415
    assert client.get("/api/settings").get_json()["port"] == 4848


def test_settings_refused_from_another_site(client, syslog):
    r = client.post("/api/settings", json={"port": 5000},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "evil.example" in syslog.wait_for("settings_rejected")
    assert client.get("/api/settings").get_json()["port"] == 4848


def test_settings_allowed_from_the_page_itself(client):
    r = client.post("/api/settings", json={"port": 5000}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200


def test_bad_settings_are_rejected_and_logged(client, syslog):
    r = client.post("/api/settings", json={"ip": "1.2.3.4?x=1"})
    assert r.status_code == 400 and "Core IP" in r.get_json()["error"]
    syslog.wait_for("settings_rejected")


def test_oversized_settings_body(client):
    r = client.post("/api/settings", json={"ip": "a" * 40000})
    assert r.status_code == 413


def test_saving_syslog_settings_repoints_syslog(client):
    sink = SyslogSink()
    try:
        r = client.post("/api/settings", json={"syslog_host": "127.0.0.1", "syslog_port": sink.port})
        assert r.status_code == 200
        assert events.active_target() == f"127.0.0.1:{sink.port}"
        r = client.post("/api/syslog/test", json={})
        assert r.get_json() == {"sent_to": f"127.0.0.1:{sink.port}"}
        assert f"version={VERSION}" in sink.wait_for("syslog_test")
    finally:
        events.configure("", 0)
        sink.close()


def test_syslog_test_when_syslog_is_off(client):
    assert client.post("/api/syslog/test", json={}).status_code == 400


def test_log_injection_is_neutralised():
    line = events.format_event("x", {"ua": 'a\nb event=forged "q"', "n": 1.25, "none": None})
    assert line == r'event=x ua="a b event=forged \"q\"" n=1.2'


# ---- WebSocket --------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_sink.py"))
    srv = make_server("127.0.0.1", 0, pa.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"ws://127.0.0.1:{srv.server_port}/ws/audio"
    srv.shutdown()
    srv.server_close()


def connect(url, origin=None):
    return simple_websocket.Client.connect(url, headers={"Origin": origin} if origin else None)


def recv(ws, timeout=5):
    msg = ws.receive(timeout=timeout)
    assert msg is not None, "server said nothing"
    return json.loads(msg)


def go_live(url, **kw):
    ws = connect(url, **kw)
    ws.send(json.dumps({"sampleRate": 48000}))
    msg = recv(ws)
    assert msg["type"] == "live", msg
    return ws


def wait_line_free(timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pa._stream_lock.locked() and relay.live_count() == 0 and pa._page is None:
            return
        time.sleep(0.02)
    raise AssertionError("the page never ended")


def test_page_start_and_stop_events(server, syslog):
    ws = go_live(server)
    for _ in range(25):
        ws.send(CHUNK)
    time.sleep(0.2)
    ws.close(1000)
    wait_line_free()
    start = syslog.wait_for("page_start")
    assert "client=127.0.0.1" in start and "sample_rate=48000" in start
    stop = syslog.wait_for("page_stop")
    assert "reason=stopped" in stop and "audio_s=0.5" in stop and "ffmpeg_exit=0" in stop
    assert re.search(r"duration_s=\d+\.\d", stop)


def test_second_phone_is_refused_while_live(server, syslog):
    a = go_live(server)
    b = connect(server)
    assert "already live" in recv(b)["msg"]
    assert "live_client=127.0.0.1" in syslog.wait_for("page_busy")
    a.close(1000)
    wait_line_free()


def test_silent_phone_ends_the_page_and_frees_the_line(server, syslog, monkeypatch):
    monkeypatch.setattr(pa, "IDLE_TIMEOUT_S", 0.5)
    ws = go_live(server)  # ...then nothing: phone locked or off Wi-Fi
    assert "No audio" in recv(ws)["msg"]
    wait_line_free()
    assert "reason=timeout" in syslog.wait_for("page_stop")
    go_live(server).close(1000)  # the next phone can go live
    wait_line_free()


def test_no_hello(server, syslog, monkeypatch):
    monkeypatch.setattr(pa, "HELLO_TIMEOUT_S", 0.3)
    ws = connect(server)
    assert "didn't start" in recv(ws)["msg"]
    wait_line_free()
    syslog.wait_for("page_rejected")


@pytest.mark.parametrize("hello,error", [
    ("garbage", "bad start message"),
    (b"\xff\xfe", "bad start message"),
    ('{"sampleRate": 1e999}', "bad start message"),
    ('{"sampleRate": 5}', "Unsupported sample rate"),
    ("[1]", "bad start message"),
])
def test_bad_hello(server, hello, error):
    ws = connect(server)
    ws.send(hello)
    assert error in recv(ws)["msg"]
    wait_line_free()


def test_other_sites_cannot_go_live(server, syslog):
    ws = connect(server, origin="https://evil.example")
    assert "another site" in recv(ws)["msg"]
    assert "origin=https://evil.example" in syslog.wait_for("page_rejected")
    assert not pa._stream_lock.locked()


def test_the_page_itself_can_go_live(server):
    # simple-websocket's client sends "Host: 127.0.0.1" without the port (browsers include
    # it), so the matching Origin here is the bare host.
    go_live(server, origin="http://127.0.0.1").close(1000)
    wait_line_free()


def test_dropped_connection(server, syslog):
    ws = go_live(server)
    ws.send(CHUNK)
    ws.sock.shutdown(socket.SHUT_RDWR)  # no close frame, just gone
    ws.sock.close()
    wait_line_free()
    assert "reason=disconnected" in syslog.wait_for("page_stop")


def test_oversized_message_ends_the_page(server, syslog):
    ws = go_live(server)
    ws.send(b"\0" * (pa.MAX_MESSAGE_BYTES + 2))
    wait_line_free(timeout=2)  # promptly, not after the idle timeout
    assert "reason=disconnected" in syslog.wait_for("page_stop")


def test_ffmpeg_failure_is_reported(server, syslog, monkeypatch):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_exits.py"))
    ws = go_live(server)
    deadline = time.monotonic() + 5
    msg = None
    while msg is None and time.monotonic() < deadline:
        try:
            ws.send(CHUNK)
        except simple_websocket.ConnectionClosed:
            pass
        msg = ws.receive(timeout=0.02)
    assert "Connection refused" in json.loads(msg)["msg"]
    wait_line_free()
    assert "Connection refused" in syslog.wait_for("page_error")
    assert "reason=error" in syslog.wait_for("page_stop")


def test_hung_ffmpeg_does_not_hold_the_line(server, syslog, monkeypatch):
    monkeypatch.setattr(relay, "STALL_S", 0.5)
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_hangs.py"))
    ws = go_live(server)
    deadline = time.monotonic() + 8
    msg = None
    while msg is None and time.monotonic() < deadline:
        try:
            ws.send(CHUNK)
        except simple_websocket.ConnectionClosed:
            pass
        msg = ws.receive(timeout=0.002)
    assert "stopped taking audio" in json.loads(msg)["msg"]
    wait_line_free()
    assert child_pids() == []


def test_shutdown_ends_the_live_page(server, syslog, monkeypatch):
    monkeypatch.setattr(pa, "_shutting_down", False)
    go_live(server)
    pa._on_exit()
    assert relay.live_count() == 0
    assert "reason=shutdown" in syslog.wait_for("page_stop")
    syslog.wait_for("service_stop")
    wait_line_free()
    assert syslog.find("page_error") == []


def test_no_threads_sockets_or_processes_leak(server):
    def counts():
        return threading.active_count(), len(os.listdir("/proc/self/fd"))

    go_live(server).close(1000)  # warm up lazily created threads (watchdog etc.)
    wait_line_free()
    time.sleep(0.5)
    threads0, fds0 = counts()

    for i in range(30):
        ws = go_live(server)
        for _ in range(5):
            ws.send(CHUNK)
        if i % 3 == 0:
            ws.close(1000)
        elif i % 3 == 1:
            ws.close(1001)
        else:
            ws.sock.shutdown(socket.SHUT_RDWR)
            ws.sock.close()
        wait_line_free()
        busy = connect(server) if i % 10 == 0 else None  # a refused phone too
        if busy:
            busy.close()

    deadline = time.monotonic() + 5
    while counts() > (threads0, fds0) and time.monotonic() < deadline:
        time.sleep(0.1)
    threads, fds = counts()
    assert threads <= threads0, f"threads {threads0} -> {threads}"
    assert fds <= fds0, f"fds {fds0} -> {fds}"
    assert child_pids() == []
