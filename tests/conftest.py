import os
import shutil
import socket
import threading
import time

import pytest

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def fake_ffmpeg(name: str) -> str:
    return os.path.join(FAKES, name)


def child_pids() -> list:
    """Processes whose parent is this one, zombies included (Linux only)."""
    me, kids = os.getpid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        if ppid == me:
            kids.append(int(pid))
    return kids


class SyslogSink:
    """A UDP syslog server on localhost that keeps every message it receives."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._closed:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.messages.append(data.decode("utf-8", "replace"))

    def find(self, event: str):
        return [m for m in self.messages if f" {event} - " in m]

    def wait_for(self, event: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.find(event)
            if found:
                return found[-1]
            time.sleep(0.02)
        raise AssertionError(f"no {event} event; got:\n" + "\n".join(self.messages))

    def close(self):
        self._closed = True
        self._thread.join()
        self.sock.close()


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    import settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "PATH", str(path))
    return path


@pytest.fixture
def syslog():
    import events
    sink = SyslogSink()
    events.configure("127.0.0.1", sink.port)
    yield sink
    events.configure("", 0)
    sink.close()
