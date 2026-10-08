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

CHUNK = b"\0\0" * 512  # 10.7 ms of 48 kHz mono s16le


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
    assert r.get_json() == {"ok": True, "version": VERSION, "live": False, "stream": "off"}
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
def core():
    """A fake Core: counts the bytes the stream sends it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    yield sock
    sock.close()


@pytest.fixture
def server(monkeypatch, core):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_udp.py"))
    pa.settings.save({"ip": "127.0.0.1", "port": core.getsockname()[1]})
    pa._stream.start()
    srv = make_server("127.0.0.1", 0, pa.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wait_for(lambda: pa._stream.state() == "up")
    yield f"ws://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()
    pa._stream.stop()
    assert child_pids() == []


def wait_for(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def connect(base, path="/ws/audio", origin=None):
    return simple_websocket.Client.connect(base + path,
                                           headers={"Origin": origin} if origin else None)


def recv(ws, timeout=5):
    msg = ws.receive(timeout=timeout)
    assert msg is not None, "server said nothing"
    return json.loads(msg)


def send(ws, **msg):
    ws.send(json.dumps(msg))


def ready(base, **kw):
    ws = connect(base, **kw)
    send(ws, type="hello", sampleRate=48000)
    msg = recv(ws)
    assert msg["type"] == "ready", msg
    return ws


def go_live(base, **kw):
    ws = ready(base, **kw)
    send(ws, type="talk")
    msg = recv(ws)
    assert msg["type"] == "live", msg
    return ws


def wait_line_free(timeout=8):
    wait_for(lambda: pa._page is None, timeout)


def test_page_start_and_stop_events(server, syslog):
    ws = go_live(server)
    for _ in range(47):
        ws.send(CHUNK)
    time.sleep(0.2)
    send(ws, type="mute")
    assert recv(ws)["type"] == "muted"
    wait_line_free()
    start = syslog.wait_for("page_start")
    assert "client=127.0.0.1" in start and "sample_rate=48000" in start
    stop = syslog.wait_for("page_stop")
    assert "reason=stopped" in stop and "audio_s=0.5" in stop and "gap_s=" in stop
    assert re.search(r"duration_s=\d+\.\d", stop)
    ws.close(1000)


def test_talker_audio_reaches_the_core(server, core):
    ws = go_live(server)
    for _ in range(10):
        ws.send(b"\x22" * 1024)
    got = bytearray()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            got += core.recv(65536)
        except socket.timeout:
            pass
    assert got.count(b"\x22") == 10 * 1024
    ws.close(1000)


def test_mute_and_go_live_again_on_one_connection(server, syslog):
    ws = go_live(server)
    for _ in range(3):
        send(ws, type="mute")
        assert recv(ws)["type"] == "muted"
        send(ws, type="talk")
        assert recv(ws)["type"] == "live"
    ws.close(1000)
    wait_line_free()
    assert len(syslog.find("page_start")) == 4


def test_second_phone_is_refused_while_live_and_can_go_live_after(server, syslog):
    a = go_live(server)
    b = ready(server)
    send(b, type="talk")
    assert "Another phone" in recv(b)["msg"]
    assert "live_client=127.0.0.1" in syslog.wait_for("page_busy")
    send(a, type="mute")
    recv(a)
    send(b, type="talk")
    assert recv(b)["type"] == "live"  # b stayed connected; no reconnect needed
    a.close(1000)
    b.close(1000)
    wait_line_free()


def test_audio_from_a_muted_phone_is_not_played(server, core):
    ws = ready(server)
    core.settimeout(0.05)
    for _ in range(10):
        ws.send(b"\x22" * 1024)
    got = bytearray()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            got += core.recv(65536)
        except socket.timeout:
            pass
    assert b"\x22" not in got
    ws.close(1000)


def test_talk_without_a_destination_is_refused(server, settings_file):
    settings_file.unlink()
    ws = ready(server)
    send(ws, type="talk")
    assert "Destination" in recv(ws)["msg"]
    ws.close(1000)


def test_silent_live_phone_is_muted(server, syslog, monkeypatch):
    monkeypatch.setattr(pa, "IDLE_TIMEOUT_S", 0.5)
    ws = go_live(server)  # ...then no audio: phone locked or off Wi-Fi
    msg = recv(ws)
    assert msg["type"] == "muted" and "No audio" in msg["msg"]
    wait_line_free()
    assert "reason=timeout" in syslog.wait_for("page_stop")
    go_live(server).close(1000)  # the next phone can go live
    ws.close(1000)
    wait_line_free()


def test_talk_time_limit_mutes_a_forgotten_phone(server, syslog):
    ws = go_live(server)
    pa._page.limit_s = 0.4  # as if Max talk were 0.4 s
    deadline = time.monotonic() + 3
    msg = None
    while msg is None and time.monotonic() < deadline:
        ws.send(CHUNK)  # still talking (or a pocket full of noise)
        raw = ws.receive(timeout=0.02)
        msg = raw and json.loads(raw)
    assert msg["type"] == "muted" and "limit set in Settings" in msg["msg"]
    assert "reason=limit" in syslog.wait_for("page_stop")
    send(ws, type="talk")
    while (m := recv(ws))["type"] == "muted":
        pass
    assert m["type"] == "live"  # going live again is allowed at once
    ws.close(1000)


def test_talk_time_limit_comes_from_settings(server):
    pa.settings.save({"max_talk_min": 2})
    ws = go_live(server)
    assert pa._page.limit_s == 120
    send(ws, type="mute")
    recv(ws)
    pa.settings.save({"max_talk_min": 0})
    send(ws, type="talk")
    recv(ws)
    assert pa._page.limit_s == 0
    ws.close(1000)


def test_vanished_phone_is_disconnected(server, monkeypatch):
    monkeypatch.setattr(pa, "SILENT_TIMEOUT_S", 0.5)
    ws = ready(server)  # ...and no pings
    assert "Lost contact" in recv(ws)["msg"]


def test_pings_keep_a_muted_phone_connected_and_are_answered(server, monkeypatch):
    monkeypatch.setattr(pa, "SILENT_TIMEOUT_S", 0.6)
    ws = ready(server)
    for i in range(5):
        send(ws, type="ping", t=i + 0.5)
        assert recv(ws) == {"type": "pong", "t": i + 0.5}
        time.sleep(0.3)
    send(ws, type="talk")
    assert recv(ws)["type"] == "live"
    ws.close(1000)


def test_link_latency_endpoint(server):
    ws = connect(server, "/ws/ping")
    for t in (1, 2.5, 1e6):
        send(ws, type="ping", t=t)
        assert recv(ws) == {"type": "pong", "t": t}
    send(ws, type="ping", t="x")  # not a number: no answer
    send(ws, type="ping", t=3)
    assert recv(ws)["t"] == 3
    ws.close(1000)


def test_link_latency_ends_when_pings_stop(server, monkeypatch):
    monkeypatch.setattr(pa, "SILENT_TIMEOUT_S", 0.3)
    ws = connect(server, "/ws/ping")
    with pytest.raises(simple_websocket.ConnectionClosed):
        for _ in range(30):
            ws.receive(timeout=0.1)


def test_link_latency_refuses_other_sites(server):
    ws = connect(server, "/ws/ping", origin="https://evil.example")
    try:
        send(ws, type="ping", t=1)
        msg = ws.receive(timeout=2)
    except simple_websocket.ConnectionClosed:
        msg = None
    assert msg is None


def test_no_hello(server, syslog, monkeypatch):
    monkeypatch.setattr(pa, "HELLO_TIMEOUT_S", 0.3)
    ws = connect(server)
    assert "didn't start" in recv(ws)["msg"]
    syslog.wait_for("page_rejected")


@pytest.mark.parametrize("hello,error", [
    ("garbage", "bad start message"),
    (b"\xff\xfe", "bad start message"),
    ("[1]", "bad start message"),
    ('{"sampleRate": 48000}', "out of date"),  # a page from before 0.6.0
    ('{"type": "hello", "sampleRate": 44100}', "Unsupported sample rate"),
])
def test_bad_hello(server, hello, error):
    ws = connect(server)
    ws.send(hello)
    assert error in recv(ws)["msg"]


def test_other_sites_cannot_connect(server, syslog):
    ws = connect(server, origin="https://evil.example")
    assert "another site" in recv(ws)["msg"]
    assert "origin=https://evil.example" in syslog.wait_for("page_rejected")
    assert pa._page is None


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


def test_page_hidden_while_live(server, syslog):
    ws = go_live(server)
    ws.close(1001)
    wait_line_free()
    assert "reason=left" in syslog.wait_for("page_stop")


def test_oversized_message_ends_the_page(server, syslog):
    ws = go_live(server)
    ws.send(b"\0" * (pa.MAX_MESSAGE_BYTES + 2))
    wait_line_free(timeout=2)  # promptly, not after the idle timeout
    assert "reason=disconnected" in syslog.wait_for("page_stop")


def test_saving_new_destination_restarts_the_stream(server, syslog):
    first = pa._stream.pid
    c = pa.app.test_client()
    r = c.post("/api/settings", json={"codec": "mp3", "syslog_host": "127.0.0.1",
                                      "syslog_port": syslog.port})  # keep syslog on the sink
    assert r.status_code == 200
    wait_for(lambda: pa._stream.pid not in (None, first) and pa._stream.state() == "up")
    assert "reason=settings" in syslog.wait_for("stream_stop")
    assert pa.app.test_client().get("/api/health").get_json()["stream"] == "up"


def test_saving_syslog_settings_does_not_restart_the_stream(server):
    first = pa._stream.pid
    pa.app.test_client().post("/api/settings", json={"syslog_port": 515})
    time.sleep(0.3)
    assert pa._stream.pid == first


def test_a_page_survives_ffmpeg_restarting(server, core, monkeypatch):
    ws = go_live(server)
    pa._stream.restart()  # as if settings changed mid-page
    wait_for(lambda: pa._stream.state() == "up")
    for _ in range(5):
        ws.send(b"\x33" * 1024)
    got = bytearray()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            got += core.recv(65536)
        except socket.timeout:
            pass
    assert b"\x33" in got
    ws.close(1000)


def test_shutdown_ends_the_live_page_and_stops_ffmpeg(server, syslog):
    go_live(server)
    pa._on_exit()
    assert relay.live_count() == 0
    assert child_pids() == []
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
    ffmpeg = child_pids()

    for i in range(30):
        ws = go_live(server)
        for _ in range(5):
            ws.send(CHUNK)
        if i % 3 == 0:
            send(ws, type="mute")
            ws.close(1000)
        elif i % 3 == 1:
            ws.close(1001)
        else:
            ws.sock.shutdown(socket.SHUT_RDWR)
            ws.sock.close()
        wait_line_free()
        if i % 10 == 0:  # a refused phone and a latency test too
            busy = go_live(server)
            other = ready(server)
            send(other, type="talk")
            recv(other)
            other.close()
            busy.close()
            p = connect(server, "/ws/ping")
            send(p, type="ping", t=1)
            recv(p)
            p.close()
            wait_line_free()

    deadline = time.monotonic() + 5
    while counts() > (threads0, fds0) and time.monotonic() < deadline:
        time.sleep(0.1)
    threads, fds = counts()
    assert threads <= threads0, f"threads {threads0} -> {threads}"
    assert fds <= fds0, f"fds {fds0} -> {fds}"
    assert child_pids() == ffmpeg  # still the one stream ffmpeg
