import socket
import time

import pytest

import relay
import settings
import stream
from conftest import child_pids, fake_ffmpeg

TICK = stream.TICK_BYTES


def ms(n):
    return stream._bytes(n)


def tone(n_bytes, value=0x11):
    return bytes([value]) * n_bytes


# ---- the jitter buffer --------------------------------------------------------

def test_silence_when_nobody_talks():
    jb = stream.JitterBuffer()
    assert jb.take() == stream.SILENCE
    assert len(jb.take()) == TICK


def test_waits_for_prefill_then_plays_in_order():
    jb = stream.JitterBuffer(prefill_ms=30)
    t = jb.open()
    jb.push(t, tone(TICK, 1) + tone(TICK, 2))   # 21 ms: not enough yet
    assert jb.take() == stream.SILENCE
    jb.push(t, tone(TICK, 3))                   # 32 ms: go
    assert jb.take() == tone(TICK, 1)
    assert jb.take() == tone(TICK, 2)
    assert jb.take() == tone(TICK, 3)
    assert jb.gap_bytes == 0  # waiting for the first audio isn't a gap


def test_a_burst_after_a_stall_is_dropped_to_stay_live():
    jb = stream.JitterBuffer(prefill_ms=30, max_ms=120)
    t = jb.open()
    jb.push(t, tone(ms(300)))   # 300 ms arriving at once, e.g. after a TCP retransmit
    assert jb.level_ms() <= 30.1
    assert jb.dropped_bytes == ms(300) - jb.prefill


def test_running_dry_inserts_silence_and_counts_the_gap():
    jb = stream.JitterBuffer(prefill_ms=30)
    t = jb.open()
    jb.push(t, tone(ms(30)))            # 2.8 ticks
    jb.take(), jb.take()
    out = jb.take()                      # only ~8.7 ms left of a 10.7 ms tick
    assert out.endswith(b"\0\0") and out.startswith(b"\x11")
    assert jb.gap_bytes > 0
    gap = jb.gap_bytes
    assert jb.take() == stream.SILENCE   # collecting PREFILL again
    assert jb.gap_bytes == gap + TICK


def test_trims_delay_that_is_never_used():
    jb = stream.JitterBuffer(prefill_ms=30, max_ms=200, slack_ms=20, trim_every_s=0.5)
    t = jb.open()
    jb.push(t, tone(ms(100)))            # a stall cleared and left 100 ms waiting
    for _ in range(jb.trim_every + 1):   # audio keeps arriving exactly on time
        jb.push(t, tone(TICK))
        jb.take()
    assert jb.level_ms() < 35            # trimmed back to about the slack
    assert jb.dropped_bytes > 0


def test_a_finished_talk_cannot_push_into_the_next():
    jb = stream.JitterBuffer()
    a = jb.open()
    jb.close(a)
    assert not jb.push(a, tone(TICK))
    b = jb.open()
    assert not jb.push(a, tone(TICK))
    assert jb.push(b, tone(TICK))


def test_the_tail_plays_out_after_mute():
    jb = stream.JitterBuffer(prefill_ms=30)
    t = jb.open()
    jb.push(t, tone(ms(40)))
    played = jb.take()
    jb.close(t)
    played += b"".join(jb.take() for _ in range(4))
    assert played.count(b"\x11") == ms(40)


def test_odd_bytes_never_misalign_samples():
    jb = stream.JitterBuffer(prefill_ms=0)
    t = jb.open()
    jb.push(t, b"\x01\x02\x03")
    assert jb.level_ms() == pytest.approx(1 / 48)


# ---- the stream ---------------------------------------------------------------

class UDPCounter:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]

    def collect(self, seconds):
        data = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data += self.sock.recv(65536)
            except socket.timeout:
                pass
        return bytes(data)


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_udp.py"))
    c = UDPCounter()
    settings.save({"ip": "127.0.0.1", "port": c.port})
    yield c
    c.sock.close()


def wait_for(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_streams_silence_in_real_time_when_nobody_talks(core, syslog):
    s = stream.Stream().start()
    try:
        wait_for(lambda: s.state() == "up")
        core.collect(0.3)                        # let it settle
        got = core.collect(2.0)
        assert abs(len(got) - 2.0 * 96000) < 0.08 * 96000, len(got)  # 48 kHz s16 mono
        assert set(got) == {0}
        assert "dest=127.0.0.1" in syslog.wait_for("stream_start")
    finally:
        s.stop()
    assert s.state() != "up"
    assert child_pids() == []
    assert "reason=shutdown" in syslog.wait_for("stream_stop")


def test_talker_audio_reaches_the_core(core):
    s = stream.Stream().start()
    try:
        wait_for(lambda: s.state() == "up")
        t = s.buffer.open()
        s.buffer.push(t, tone(ms(100), 0x22))
        got = core.collect(0.5)
        assert got.count(b"\x22") == ms(100)
    finally:
        s.stop()


def test_nothing_is_streamed_until_a_destination_is_saved(monkeypatch, settings_file):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_udp.py"))
    s = stream.Stream().start()
    try:
        time.sleep(0.3)
        assert s.state() == "off" and child_pids() == []
        settings.save({"ip": "127.0.0.1", "port": 9})
        s.restart()
        wait_for(lambda: s.state() == "up")
    finally:
        s.stop()
    assert child_pids() == []


def test_new_settings_restart_ffmpeg(core, syslog):
    s = stream.Stream().start()
    try:
        wait_for(lambda: s.state() == "up")
        first = s.pid
        settings.save({"codec": "mp3"})
        s.restart()
        wait_for(lambda: s.pid not in (None, first))
        assert s.cfg["codec"] == "mp3"
        assert "reason=settings" in syslog.wait_for("stream_stop")
    finally:
        s.stop()
    assert child_pids() == []


def test_a_dying_ffmpeg_is_restarted(core, syslog, monkeypatch):
    monkeypatch.setattr(stream, "RESTART_MIN_S", 0.2)
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_exits.py"))
    s = stream.Stream().start()
    try:
        assert "Connection refused" in syslog.wait_for("stream_error")
        assert s.state() == "down"
        monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_udp.py"))
        wait_for(lambda: s.state() == "up")
        assert len(core.collect(0.3)) > 0
    finally:
        s.stop()
    assert child_pids() == []


def test_a_hung_ffmpeg_is_replaced(core, syslog, monkeypatch):
    monkeypatch.setattr(relay, "STALL_S", 0.3)
    monkeypatch.setattr(stream, "RESTART_MIN_S", 0.2)
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_hangs.py"))
    s = stream.Stream().start()
    try:
        assert "stopped taking audio" in syslog.wait_for("stream_error", timeout=10)
        monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg("ffmpeg_udp.py"))
        wait_for(lambda: s.state() == "up", timeout=10)
    finally:
        s.stop()
    assert child_pids() == []
