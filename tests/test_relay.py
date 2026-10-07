import os
import threading
import time

import pytest

import relay
from conftest import child_pids, fake_ffmpeg, needs_ffmpeg

CFG = {"ip": "127.0.0.1", "port": 9, "codec": "pcm", "bitrate": 128}
CHUNK = b"\0\0" * 960  # 20 ms of 48 kHz mono s16le


def use(monkeypatch, fake):
    monkeypatch.setattr(relay, "FFMPEG_BIN", fake_ffmpeg(fake))


def assert_cleaned_up(r):
    """No process, zombie, thread or pipe left behind."""
    assert r.proc.returncode is not None, "ffmpeg not reaped"
    assert r.proc.pid not in child_pids()
    assert r.proc.stdin.closed and r.proc.stderr.closed
    assert not r._writer.is_alive() and not r._reader.is_alive()
    assert r not in relay._live


def test_pcm_command():
    cmd = relay.build_cmd(CFG, 48000)
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16be"
    assert cmd[-1] == "rtp://127.0.0.1:9?pkt_size=1472"


def test_mp3_packets_hold_exactly_one_frame():
    cmd = relay.build_cmd({**CFG, "codec": "mp3"}, 48000)
    # 144 * 128000 / 48000 = 384, +1 padding, plus RTP (12) + MPA (4) headers + 8 slack.
    assert cmd[-1].endswith("?pkt_size=409")


def test_ipv6_destination_is_bracketed():
    assert relay.build_cmd({**CFG, "ip": "fe80::1"}, 48000)[-1].startswith("rtp://[fe80::1]:9?")


@needs_ffmpeg
def test_real_ffmpeg_start_write_stop():
    r = relay.FFmpegRelay(CFG, 48000).start()
    for _ in range(50):
        r.write(CHUNK)
    r.stop()
    assert r.returncode == 0
    assert_cleaned_up(r)


def test_missing_ffmpeg_is_a_relay_error(monkeypatch):
    monkeypatch.setattr(relay, "FFMPEG_BIN", "/nonexistent/ffmpeg")
    with pytest.raises(relay.RelayError, match="not found"):
        relay.FFmpegRelay(CFG, 48000).start()


def test_stop_twice_and_from_two_threads(monkeypatch):
    use(monkeypatch, "ffmpeg_sink.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    r.write(CHUNK)
    threads = [threading.Thread(target=r.stop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    r.stop()
    assert r.returncode == 0
    assert_cleaned_up(r)


def test_writing_after_stop_raises(monkeypatch):
    use(monkeypatch, "ffmpeg_sink.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    r.stop()
    with pytest.raises(relay.RelayError):
        r.write(CHUNK)


def test_ffmpeg_dying_surfaces_its_error(monkeypatch):
    use(monkeypatch, "ffmpeg_exits.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    deadline = time.monotonic() + 10
    with pytest.raises(relay.RelayError, match="Connection refused"):
        while time.monotonic() < deadline:
            r.write(CHUNK)
            time.sleep(0.01)
    r.stop()
    assert r.returncode == 1
    assert_cleaned_up(r)


def test_hung_ffmpeg_is_detected_and_killed(monkeypatch):
    monkeypatch.setattr(relay, "STALL_S", 0.5)
    use(monkeypatch, "ffmpeg_hangs.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    deadline = time.monotonic() + 10
    with pytest.raises(relay.RelayError, match="stopped taking audio"):
        while time.monotonic() < deadline:
            r.write(CHUNK)
            time.sleep(0.002)
    assert r.dropped_bytes > 0

    t0 = time.monotonic()
    r.stop()  # it ignores SIGTERM, so this has to escalate to SIGKILL
    assert time.monotonic() - t0 < 5
    assert r.returncode == -9
    assert_cleaned_up(r)


def test_write_never_blocks_the_caller(monkeypatch):
    monkeypatch.setattr(relay, "STALL_S", 1000)
    use(monkeypatch, "ffmpeg_hangs.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    try:
        slowest = 0.0
        for _ in range(300):  # 6 s of audio into a process that reads nothing
            t0 = time.monotonic()
            r.write(CHUNK)
            slowest = max(slowest, time.monotonic() - t0)
        assert slowest < 0.05
        assert r.dropped_bytes > 0
    finally:
        r.stop()
    assert_cleaned_up(r)


def test_watchdog_stops_a_relay_nobody_feeds(monkeypatch, syslog):
    monkeypatch.setattr(relay, "REAP_AFTER_S", 0.2)
    use(monkeypatch, "ffmpeg_sink.py")
    r = relay.FFmpegRelay(CFG, 48000).start()
    r.write(CHUNK)
    deadline = time.monotonic() + 5
    while r in relay._live and time.monotonic() < deadline:
        relay.reap_once()  # the background watchdog may get there first; either is fine
        time.sleep(0.05)
    syslog.wait_for("relay_reaped")
    assert_cleaned_up(r)


def test_stop_all(monkeypatch):
    use(monkeypatch, "ffmpeg_sink.py")
    relays = [relay.FFmpegRelay(CFG, 48000).start() for _ in range(3)]
    assert relay.stop_all() == 3
    for r in relays:
        assert_cleaned_up(r)
    assert relay.live_count() == 0
