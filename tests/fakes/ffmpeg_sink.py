#!/usr/bin/env python3
"""Stand-in for ffmpeg that reads stdin to EOF and exits 0, like ffmpeg does."""
import sys

while sys.stdin.buffer.read(65536):
    pass
