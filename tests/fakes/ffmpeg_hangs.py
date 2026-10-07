#!/usr/bin/env python3
"""Stand-in for a wedged ffmpeg: never reads its input and ignores SIGTERM. Only SIGKILL works."""
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(120)
