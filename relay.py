"""One ffmpeg process per streaming session.

Browser -> raw 16-bit mono PCM -> ffmpeg stdin -> MP3 (or L16) over RTP -> Q-SYS Core.
"""

import collections
import logging
import math
import os
import subprocess
import threading

log = logging.getLogger("relay")

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")


class RelayError(RuntimeError):
    pass


def _mp3_frame_bytes(bitrate_kbps: int, sample_rate: int) -> int:
    # MPEG-1 Layer III frame size, +1 for the optional padding byte.
    return math.floor(144 * bitrate_kbps * 1000 / sample_rate) + 1


def build_cmd(cfg: dict, sample_rate: int) -> list:
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "warning",
        # Raw PCM from the browser. The format is fully specified, so skip stream
        # analysis: without these two flags ffmpeg buffers ~1 s before encoding.
        # (Do NOT add -fflags nobuffer: on ffmpeg 6.x it breaks the raw demuxer.)
        "-probesize", "32", "-analyzeduration", "0",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
    ]

    if cfg["codec"] == "pcm":
        # L16 stereo @ 44.1 kHz is RTP static payload type 10, so the Core can
        # decode it without an SDP. Q-SYS only accepts 2- or 6-channel PCM.
        cmd += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16be"]
        pkt_size = 1472  # default MTU-sized packets; each one is ~8 ms of audio
    else:
        bitrate = int(cfg.get("bitrate", 128))
        cmd += ["-ac", "1", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
                # No bit reservoir: each frame is self-contained, slightly lower latency.
                "-reservoir", "0"]
        # ffmpeg's RTP muxer packs as many MP3 frames as fit in one packet, which
        # would add ~50 ms. Size the packet so exactly one frame fits.
        frame = _mp3_frame_bytes(bitrate, sample_rate)
        pkt_size = 12 + 4 + frame + 8  # RTP header + MPA header + frame + slack

    cmd += [
        "-flush_packets", "1",
        "-f", "rtp", f"rtp://{cfg['ip']}:{cfg['port']}?pkt_size={pkt_size}",
    ]
    return cmd


class FFmpegRelay:
    def __init__(self, cfg: dict, sample_rate: int):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.cmd = build_cmd(cfg, sample_rate)
        self.proc = None
        self._stderr_tail = collections.deque(maxlen=20)

    @property
    def destination(self) -> str:
        return f"rtp://{self.cfg['ip']}:{self.cfg['port']}"

    def start(self):
        log.info("starting: %s", " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,  # unbuffered: every write goes straight to ffmpeg
            )
        except FileNotFoundError:
            raise RelayError(f"ffmpeg not found (looked for '{FFMPEG_BIN}')")
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self

    def _pump_stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and "Guessed Channel Layout" not in line:
                self._stderr_tail.append(line)
                log.warning("ffmpeg: %s", line)

    def write(self, data: bytes):
        try:
            self.proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            raise RelayError("ffmpeg exited: " + (self.error_tail() or "no error output"))

    def error_tail(self) -> str:
        return " | ".join(self._stderr_tail)

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        log.info("stopped (exit %s)", self.proc.returncode)
