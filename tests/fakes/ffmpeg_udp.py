#!/usr/bin/env python3
"""Stand-in for ffmpeg that sends its raw input, as read, to the rtp:// port in its last
argument, so tests can see exactly what and when the stream feeds it. Exits 0 at EOF."""
import os
import socket
import sys
from urllib.parse import urlsplit

url = urlsplit(sys.argv[-1])
out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
while True:
    data = os.read(0, 65536)
    if not data:
        break
    out.sendto(data, (url.hostname, url.port))
