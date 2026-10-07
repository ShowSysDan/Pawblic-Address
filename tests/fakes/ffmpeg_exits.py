#!/usr/bin/env python3
"""Stand-in for ffmpeg that fails straight away with an error, like a bad destination."""
import sys

sys.stderr.write("rtp://10.255.0.1:4848: Connection refused\n")
sys.exit(1)
