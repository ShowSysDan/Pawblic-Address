#!/usr/bin/env python3
"""Soak / leak test against the real server and real ffmpeg (Linux).

    python tests/soak.py                 # 300 pages + a 60 s page, then crash tests
    python tests/soak.py --cycles 1000 --long 600

Starts app.py as a separate process, drives pages through it in every way a page can
end (Stop, tab closed, Wi-Fi drop, silence, bad hello, oversized message, a second
phone), and samples the server's memory, threads, open files and ffmpeg children as
it goes. Then checks SIGTERM and SIGKILL during a live page leave no ffmpeg behind.
Exits non-zero if anything leaked.
"""

import argparse
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import simple_websocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = b"\0\0" * 960  # 20 ms of 48 kHz mono s16le
IDLE_TIMEOUT_S = 5     # app.IDLE_TIMEOUT_S
RSS_GROWTH_LIMIT_KB = 4096

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


class UDPSink:
    """Counts datagrams; keeps the text ones (syslog)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.packets = 0
        self.texts = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.packets += 1
            if data[:1] == b"<":
                self.texts.append(data.decode("utf-8", "replace"))

    def count(self, event):
        return sum(f" {event} - " in t for t in self.texts)

    def find(self, event):
        return [t for t in self.texts if f" {event} - " in t]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def proc_stat(pid):
    """(state, ppid) from /proc, or None if the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    fields = stat[stat.rindex(")") + 2:].split()
    return fields[0], int(fields[1])


def children(pid):
    kids = []
    for p in filter(str.isdigit, os.listdir("/proc")):
        st = proc_stat(p)
        if st and st[1] == pid:
            kids.append(int(p))
    return kids


def metrics(pid):
    with open(f"/proc/{pid}/status") as f:
        status = dict(line.split(":", 1) for line in f if ":" in line)
    return {
        "rss_kb": int(status["VmRSS"].split()[0]),
        "threads": int(status["Threads"]),
        "fds": len(os.listdir(f"/proc/{pid}/fd")),
        "ffmpeg": len(children(pid)),
    }


class Server:
    def __init__(self, rtp_port, syslog_port):
        self.dir = tempfile.mkdtemp(prefix="pa-soak-")
        settings_path = os.path.join(self.dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"ip": "127.0.0.1", "port": rtp_port,
                       "syslog_host": "127.0.0.1", "syslog_port": syslog_port}, f)
        self.port = free_port()
        self.log = open(os.path.join(self.dir, "server.log"), "w")
        env = {**os.environ, "PORT": str(self.port), "SETTINGS_FILE": settings_path}
        self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.base = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.base is None:
            for scheme in ("http", "https"):
                try:
                    url = f"{scheme}://127.0.0.1:{self.port}"
                    urllib.request.urlopen(url + "/api/health", timeout=1, context=_TLS)
                    self.base = url
                    break
                except OSError:
                    pass
            time.sleep(0.2)
        if self.base is None:
            raise SystemExit(f"server didn't start; see {self.log.name}")
        self.ws_url = self.base.replace("http", "ws", 1) + "/ws/audio"

    @property
    def pid(self):
        return self.proc.pid

    def health(self):
        with urllib.request.urlopen(self.base + "/api/health", timeout=3, context=_TLS) as r:
            return json.load(r)

    def connect(self):
        return simple_websocket.Client.connect(self.ws_url, ssl_context=_TLS)

    def go_live(self):
        ws = self.connect()
        ws.send(json.dumps({"sampleRate": 48000}))
        msg = json.loads(ws.receive(timeout=5))
        if msg.get("type") != "live":
            raise RuntimeError(f"not live: {msg}")
        return ws

    def wait_idle(self, timeout=8):
        """Line free and no ffmpeg running. Returns seconds taken, or None on timeout."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not self.health()["live"] and not children(self.pid):
                return time.monotonic() - t0
            time.sleep(0.02)
        return None


def drop(ws):
    ws.sock.shutdown(socket.SHUT_RDWR)
    ws.sock.close()


def close(ws, code=1000):
    try:
        ws.close(code)
    except simple_websocket.ConnectionClosed:  # the server got there first
        pass


def scenario(srv, i):
    kind = i % 20
    if kind <= 11:                       # normal page, Stop tapped
        ws = srv.go_live()
        for _ in range(50):
            ws.send(CHUNK)
        close(ws)
        return "stop"
    if kind <= 14:                       # Wi-Fi drop mid-page
        ws = srv.go_live()
        for _ in range(25):
            ws.send(CHUNK)
        drop(ws)
        return "drop"
    if kind == 15:                       # tab closed
        ws = srv.go_live()
        ws.send(CHUNK)
        close(ws, 1001)
        return "left"
    if kind == 16:                       # garbage instead of hello
        ws = srv.connect()
        ws.send(b"\xff" * 100)
        ws.receive(timeout=5)
        return "bad-hello"
    if kind == 17:                       # oversized message
        ws = srv.go_live()
        ws.send(b"\0" * (70 * 1024))
        time.sleep(0.1)
        return "oversize"
    if kind == 18:                       # second phone refused
        a = srv.go_live()
        b = srv.connect()
        b.receive(timeout=5)
        close(b)
        close(a)
        return "busy"
    ws = srv.go_live()                   # real-time pace
    for _ in range(25):
        ws.send(CHUNK)
        time.sleep(0.02)
    close(ws)
    return "realtime"


def row(label, m, base=None):
    delta = f"  ({m['rss_kb'] - base['rss_kb']:+d} KB)" if base else ""
    print(f"  {label:<22} rss {m['rss_kb']:>7} KB{delta:<14} threads {m['threads']:>3}  "
          f"fds {m['fds']:>3}  ffmpeg {m['ffmpeg']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=300)
    ap.add_argument("--long", type=int, default=60, help="seconds for one long real-time page")
    args = ap.parse_args()
    problems = []

    rtp, syslog = UDPSink(), UDPSink()
    srv = Server(rtp.port, syslog.port)
    print(f"server pid {srv.pid} at {srv.base}, log {srv.log.name}\n")

    for i in range(20):  # warm-up: imports, caches, lazily started threads
        scenario(srv, i)
        srv.wait_idle()
    time.sleep(1.5)
    base = metrics(srv.pid)
    print(f"{args.cycles} pages, mixed endings:")
    row("baseline", base)

    kinds, slow = {}, []
    for i in range(args.cycles):
        kind = scenario(srv, i)
        kinds[kind] = kinds.get(kind, 0) + 1
        took = srv.wait_idle()
        if took is None:
            problems.append(f"page {i} ({kind}) never ended")
        elif took > 3:
            slow.append(kind)
        if (i + 1) % 50 == 0:
            row(f"after {i + 1}", metrics(srv.pid), base)
    print("  endings: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))

    print("\nSilent phone (no audio after going live), twice:")
    for _ in range(2):
        ws = srv.go_live()
        t0 = time.monotonic()
        msg = ws.receive(timeout=IDLE_TIMEOUT_S + 5)
        took = srv.wait_idle()
        ended = time.monotonic() - t0
        print(f"  ended after {ended:.1f} s: {json.loads(msg)['msg'] if msg else 'no message'}")
        if took is None or ended > IDLE_TIMEOUT_S + 3:
            problems.append("silent phone didn't end the page in time")

    if args.long:
        print(f"\nOne {args.long} s page at real-time pace:")
        ws = srv.go_live()
        t0 = time.monotonic()
        start_rss = None
        next_sample, sent = t0, 0
        while time.monotonic() - t0 < args.long:
            ws.send(CHUNK)
            sent += 1
            if time.monotonic() >= next_sample:
                m = metrics(srv.pid)
                start_rss = start_rss or m
                row(f"t={time.monotonic() - t0:4.0f} s", m, start_rss)
                if m["ffmpeg"] != 1:
                    problems.append(f"{m['ffmpeg']} ffmpeg processes during one page")
                next_sample += 10
            time.sleep(max(0.0, t0 + sent * 0.02 - time.monotonic()))
        close(ws)
        if srv.wait_idle() is None:
            problems.append("long page never ended")
        row("end of long page", metrics(srv.pid), start_rss)

    time.sleep(2)
    end = metrics(srv.pid)
    print("\nAfter everything, settled:")
    row("baseline", base)
    row("now", end, base)
    if end["ffmpeg"]:
        problems.append(f"{end['ffmpeg']} ffmpeg processes left running")
    if end["threads"] > base["threads"]:
        problems.append(f"threads grew {base['threads']} -> {end['threads']}")
    if end["fds"] > base["fds"]:
        problems.append(f"open files grew {base['fds']} -> {end['fds']}")
    if end["rss_kb"] - base["rss_kb"] > RSS_GROWTH_LIMIT_KB:
        problems.append(f"memory grew {end['rss_kb'] - base['rss_kb']} KB")

    starts, stops = syslog.count("page_start"), syslog.count("page_stop")
    print(f"\nSyslog: {len(syslog.texts)} messages, page_start {starts}, page_stop {stops}, "
          f"page_error {syslog.count('page_error')}; RTP packets to the fake Core: {rtp.packets}")
    if starts != stops:
        problems.append(f"page_start {starts} != page_stop {stops}")
    if slow:
        print(f"  {len(slow)} pages took over 3 s to clean up: {', '.join(slow)}")

    print("\nSIGTERM while live:")
    ws = srv.go_live()
    ws.send(CHUNK)
    ffmpeg = children(srv.pid)
    srv.proc.send_signal(signal.SIGTERM)
    try:
        srv.proc.wait(10)
    except subprocess.TimeoutExpired:
        problems.append("server ignored SIGTERM")
        srv.proc.kill()
    time.sleep(0.5)
    alive = [p for p in ffmpeg if (proc_stat(p) or ("Z",))[0] != "Z"]
    shutdown_stop = [t for t in syslog.find("page_stop") if "reason=shutdown" in t]
    print(f"  server exit {srv.proc.returncode}; ffmpeg {ffmpeg} left running: {alive}; "
          f"page_stop reason=shutdown: {bool(shutdown_stop)}; service_stop: "
          f"{bool(syslog.find('service_stop'))}")
    if alive:
        problems.append("ffmpeg survived SIGTERM")
    if not shutdown_stop or not syslog.find("service_stop"):
        problems.append("no shutdown events on SIGTERM")

    print("\nSIGKILL while live (server crash):")
    srv = Server(rtp.port, syslog.port)
    ws = srv.go_live()
    for _ in range(10):
        ws.send(CHUNK)
    ffmpeg = children(srv.pid)
    srv.proc.kill()
    srv.proc.wait()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alive = [p for p in ffmpeg if (proc_stat(p) or ("Z",))[0] != "Z"]
        if not alive:
            break
        time.sleep(0.05)
    print(f"  orphaned ffmpeg {ffmpeg} still running after the crash: {alive}")
    if alive:
        problems.append("ffmpeg outlived a crashed server")
        for p in alive:
            os.kill(p, signal.SIGKILL)

    print("\n" + ("PASS" if not problems else "FAIL:\n  " + "\n  ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
