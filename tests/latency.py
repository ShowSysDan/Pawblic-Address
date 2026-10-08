#!/usr/bin/env python3
"""Measure the server's share of the delay, with real ffmpeg (Linux, PCM).

    python tests/latency.py

Starts app.py, goes live like a phone, and sends real-time audio with a click every
250 ms. Times each click from the WebSocket message that carried it to the RTP packet
that carries it out. Halfway through it imitates a Wi-Fi stall: 300 ms of nothing, then
all of it at once, as TCP delivers after a retransmit. Reports the delay before and after
the stall, and the longest gap between RTP packets (what the Core's buffer has to cover).

This is the server only. The phone's audio stack, the Wi-Fi and the Core's receive
buffer come on top; the page's Connection test measures the network part.
"""

import json
import os
import socket
import statistics
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from soak import CHUNK_S, Server, close, expect  # noqa: E402

SAMPLES = 512
CLICK_EVERY_S = 0.25
STALL_AT_S, STALL_S = 3.0, 0.3
RUN_S = 8.0


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.5)
    packets = []  # (arrival, payload)
    done = threading.Event()

    def receive():
        while not done.is_set():
            try:
                data = rx.recv(2048)
            except socket.timeout:
                continue
            packets.append((time.monotonic(), data[12:]))

    threading.Thread(target=receive, daemon=True).start()
    srv = Server(rx.getsockname()[1], 9)
    try:
        ws = srv.go_live()
        silence = b"\0\0" * SAMPLES
        click = struct.pack("<h", 30000) + silence[2:]
        sent = []  # times a click left
        t0 = time.monotonic()
        n, stalled = 0, False
        every = round(CLICK_EVERY_S / CHUNK_S)
        while n * CHUNK_S < RUN_S:
            due = t0 + n * CHUNK_S
            if not stalled and n * CHUNK_S >= STALL_AT_S:
                stalled = True  # hold everything for STALL_S, then send it in one go
                due += STALL_S
            time.sleep(max(0.0, due - time.monotonic()))
            is_click = n % every == every // 2
            ws.send(click if is_click else silence)
            if is_click:
                sent.append(time.monotonic())
            n += 1
        time.sleep(0.5)
        ws.send(json.dumps({"type": "mute"}))
        expect(ws, "muted")
        close(ws)
    finally:
        done.set()
        srv.proc.terminate()
        srv.proc.wait(10)

    # Find clicks in the L16 (big-endian stereo) packets; match each to the last one sent.
    found, last = [], -1.0
    for at, payload in packets:
        loud = any(abs(v) > 8000 for (v,) in struct.iter_unpack(">h", payload[:len(payload) & ~1]))
        if loud and at - last > 0.1:
            last = at
            before = [s for s in sent if s <= at]
            if before:
                found.append((before[-1], at - before[-1]))
    stall = t0 + STALL_AT_S
    before = [d * 1000 for s, d in found if s < stall - 0.05]
    after = [d * 1000 for s, d in found if s > stall + STALL_S + 1.5]
    arrivals = [a for a, _ in packets if t0 + 0.5 < a < t0 + RUN_S]
    gap = max(b - a for a, b in zip(arrivals, arrivals[1:])) * 1000

    def show(label, xs):
        if xs:
            print(f"  {label:<30} median {statistics.median(xs):5.1f} ms, max {max(xs):5.1f} ms "
                  f"({len(xs)} clicks)")
        else:
            print(f"  {label:<30} no clicks found")

    print("\nServer delay, WebSocket message in -> RTP packet out:")
    show("before the stall", before)
    show("1.5 s after a 300 ms stall", after)
    print(f"  clicks sent {len(sent)}, heard {len(found)} (the stall's late audio is dropped)")
    print(f"  longest gap between RTP packets: {gap:.1f} ms")


if __name__ == "__main__":
    main()
