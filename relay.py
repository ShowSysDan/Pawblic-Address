"""One ffmpeg process per page (streaming session).

Browser -> raw 16-bit mono PCM -> ffmpeg stdin -> MP3 (or L16) over RTP -> Q-SYS Core.

Every ffmpeg process belongs to an FFmpegRelay and ends with stop(), which is safe to
call twice and always finishes in bounded time. Nothing here blocks the caller: audio
goes through a small queue to a writer thread, so a hung ffmpeg can't stall the
WebSocket, and a watchdog stops any relay whose owner has stopped feeding it.
"""

import collections
import logging
import math
import os
import queue
import subprocess
import threading
import time

import events

log = logging.getLogger("pa.relay")

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

QUEUE_CHUNKS = 25     # audio waiting for ffmpeg (~0.5 s of 20 ms chunks); beyond this, drop
STALL_S = 2.0         # one write to ffmpeg blocked this long: ffmpeg is hung, give up
REAP_AFTER_S = 15.0   # a live relay nobody has fed for this long has lost its owner
REAP_EVERY_S = 1.0
STOP_STEP_S = 1.0     # per step when stopping: flush, then terminate, then kill

_live = set()
_live_lock = threading.Lock()
_reaper = None


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

    host = f"[{cfg['ip']}]" if ":" in cfg["ip"] else cfg["ip"]  # IPv6 needs brackets
    cmd += [
        "-flush_packets", "1",
        "-f", "rtp", f"rtp://{host}:{cfg['port']}?pkt_size={pkt_size}",
    ]
    return cmd


class FFmpegRelay:
    def __init__(self, cfg: dict, sample_rate: int):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.cmd = build_cmd(cfg, sample_rate)
        self.proc = None
        self.dropped_bytes = 0
        self.last_fed = time.monotonic()
        self._queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self._stderr_tail = collections.deque(maxlen=20)
        self._writer = None
        self._reader = None
        self._write_started = None  # set while the writer is inside a write to ffmpeg
        self._broken = False
        self._stopped = False
        self._stop_lock = threading.Lock()

    @property
    def destination(self) -> str:
        return f"rtp://{self.cfg['ip']}:{self.cfg['port']}"

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    @property
    def returncode(self):
        return self.proc.returncode if self.proc else None

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
        except OSError as e:
            raise RelayError(f"could not start ffmpeg ('{FFMPEG_BIN}'): {e}")
        self._writer = threading.Thread(target=self._pump_stdin, daemon=True,
                                        name=f"ffmpeg-{self.proc.pid}-stdin")
        self._reader = threading.Thread(target=self._pump_stderr, daemon=True,
                                        name=f"ffmpeg-{self.proc.pid}-stderr")
        self._writer.start()
        self._reader.start()
        with _live_lock:
            _live.add(self)
        _ensure_reaper()
        return self

    def _pump_stdin(self):
        fd = self.proc.stdin.fileno()
        while True:
            data = self._queue.get()
            if data is None:
                return
            self._write_started = time.monotonic()
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            except OSError:  # BrokenPipeError: ffmpeg exited or was killed
                self._broken = True
                return
            finally:
                self._write_started = None

    def _pump_stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and "Guessed Channel Layout" not in line:
                self._stderr_tail.append(line)
                log.warning("ffmpeg: %s", line)

    def write(self, data: bytes):
        """Queue audio for ffmpeg. Never blocks: if ffmpeg falls behind, audio is dropped."""
        now = self.last_fed = time.monotonic()
        if self._stopped:
            raise RelayError("the relay was stopped")
        if self._broken or self.proc.poll() is not None:
            self._reader.join(0.5)  # let the last error lines arrive
            raise RelayError("ffmpeg exited: " + (self.error_tail() or "no error output"))
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self.dropped_bytes += len(data)
            started = self._write_started
            if started is not None and now - started > STALL_S:
                raise RelayError(f"ffmpeg stopped taking audio for {STALL_S:g} s")

    def error_tail(self) -> str:
        return " | ".join(self._stderr_tail)

    def stop(self):
        """End the ffmpeg process. Safe to call more than once and from any thread."""
        with self._stop_lock:
            if self._stopped or self.proc is None:
                return
            self._stopped = True
        with _live_lock:
            _live.discard(self)

        # 1. Let the writer finish what's queued; closing stdin then makes ffmpeg flush and exit.
        self._tell_writer_to_finish()
        self._writer.join(STOP_STEP_S)
        if self._writer.is_alive():
            # Stuck in a write because ffmpeg isn't reading. Killing ffmpeg breaks the pipe.
            self._kill()
            self._writer.join(STOP_STEP_S)
        # Never close a pipe another thread may still be using: its fd number could be reused.
        if not self._writer.is_alive():
            self._close(self.proc.stdin)

        # 2. Wait for ffmpeg to exit, escalating to terminate and then kill.
        try:
            self.proc.wait(STOP_STEP_S)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(STOP_STEP_S)
            except subprocess.TimeoutExpired:
                self._kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    log.error("ffmpeg pid %s did not die after SIGKILL", self.proc.pid)

        self._reader.join(STOP_STEP_S)
        if not self._reader.is_alive():
            self._close(self.proc.stderr)
        log.info("stopped pid %s (exit %s)", self.proc.pid, self.proc.returncode)

    def _tell_writer_to_finish(self):
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except queue.Full:  # only when ffmpeg is stuck; that audio is lost anyway
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def _kill(self):
        try:
            self.proc.kill()
        except OSError:
            pass

    @staticmethod
    def _close(pipe):
        try:
            pipe.close()
        except OSError:
            pass


def live_count() -> int:
    with _live_lock:
        return len(_live)


def stop_all() -> int:
    """Stop every live relay (server shutdown). Returns how many there were."""
    with _live_lock:
        relays = list(_live)
    for r in relays:
        r.stop()
    return len(relays)


def reap_once() -> int:
    """Stop relays nobody has fed for REAP_AFTER_S. Returns how many were stopped."""
    now = time.monotonic()
    with _live_lock:
        stale = [r for r in _live if now - r.last_fed > REAP_AFTER_S]
    for r in stale:
        events.emit("relay_reaped", level=logging.ERROR, pid=r.pid,
                    idle_s=round(now - r.last_fed, 1), dest=r.destination)
        r.stop()
    return len(stale)


def _reap_forever():
    while True:
        time.sleep(REAP_EVERY_S)
        try:
            reap_once()
        except Exception:  # the watchdog must outlive any one bad relay
            log.exception("watchdog")


def _ensure_reaper():
    global _reaper
    with _live_lock:
        if _reaper is None or not _reaper.is_alive():
            _reaper = threading.Thread(target=_reap_forever, name="relay-watchdog", daemon=True)
            _reaper.start()
