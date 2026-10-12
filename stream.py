"""The always-on stream to the Core: one ffmpeg, fed from the server's own clock.

Every tick (512 samples, about 10.7 ms) the feeder thread hands ffmpeg exactly one tick of
audio: the live phone's, from a small jitter buffer; otherwise the recorded announcement
that is playing, from the announcement queue; otherwise silence.
So the Core receives one steady, unbroken RTP stream from the moment the server starts:

- going live costs nothing on the Core's side: no new ffmpeg, no new RTP stream to lock on to;
- however bumpy the phone's Wi-Fi is, the Core never sees a gap or a burst. Late audio is
  dropped here, in a buffer we control, instead of piling up in the Core's receive buffer
  and making everything after it late.

The stream starts once a destination has been saved, restarts with new settings when they
change, and restarts by itself (with backoff) if ffmpeg dies. ffmpeg is always started and
stopped by relay.FFmpegRelay, in a finally.
"""

import collections
import itertools
import logging
import threading
import time

import events
import relay
import settings

log = logging.getLogger("pa.stream")

RATE = relay.SAMPLE_RATE
TICK_SAMPLES = 512             # half of ffmpeg's 1024-sample input packet, so ticks line up with it
TICK_BYTES = TICK_SAMPLES * 2  # 16-bit mono
TICK_S = TICK_SAMPLES / RATE
SILENCE = bytes(TICK_BYTES)

PREFILL_MS = 30        # audio to collect before playing a talker (and again after a gap)
MAX_MS = 120           # more than this waiting means a Wi-Fi stall just cleared: drop back to PREFILL_MS
TRIM_EVERY_S = 1.0     # how often to look for latency that can be trimmed
TRIM_SLACK_MS = 20     # ...keeping this much in hand at the lowest point of the last TRIM_EVERY_S
RESYNC_AFTER_S = 0.1   # if the feeder itself falls this far behind, restart its clock rather than burst
RESTART_MIN_S = 1.0    # wait before restarting a failed ffmpeg; doubles up to RESTART_MAX_S
RESTART_MAX_S = 30.0
STOP_TIMEOUT_S = 10.0
MAX_ANNOUNCE_S = 120   # longest recorded announcement accepted
MIN_ANNOUNCE_S = 0.3   # shorter than this is a mis-tap, not an announcement
MAX_QUEUED = 5         # announcements waiting to play, besides the one playing


def _bytes(ms: float) -> int:
    return int(ms * RATE / 1000) * 2


class JitterBuffer:
    """Audio from the talking phone, waiting for the feeder. Never more than MAX_MS.

    Each talk gets a token from open(); push() with a stale token is ignored, so a phone
    whose talk has ended can't leak audio into the next one. After close() whatever is
    left still plays out, so the last word isn't clipped.
    """

    def __init__(self, prefill_ms=PREFILL_MS, max_ms=MAX_MS, slack_ms=TRIM_SLACK_MS,
                 trim_every_s=TRIM_EVERY_S):
        self.prefill = _bytes(prefill_ms)
        self.max = max(_bytes(max_ms), self.prefill + TICK_BYTES)
        self.slack = _bytes(slack_ms)
        self.trim_every = max(1, round(trim_every_s / TICK_S))
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._token = 0
        self._talking = False
        self._playing = False
        self._heard = False  # this talk has started playing, so running dry is a gap
        self._low = None     # lowest level since the last trim
        self._ticks = 0
        self.dropped_bytes = 0
        self.gap_bytes = 0

    def level_ms(self) -> float:
        with self._lock:
            return len(self._buf) / 2 / RATE * 1000

    def open(self) -> int:
        with self._lock:
            self._token += 1
            self._talking = True
            self._heard = False
            self.dropped_bytes = self.gap_bytes = 0
            return self._token

    def close(self, token: int) -> tuple:
        """End a talk. Returns (dropped_bytes, gap_bytes) for it."""
        with self._lock:
            if token == self._token:
                self._talking = False
            return self.dropped_bytes, self.gap_bytes

    def push(self, token: int, data: bytes) -> bool:
        with self._lock:
            if token != self._token or not self._talking:
                return False
            self._buf += data[:len(data) & ~1]
            if len(self._buf) > self.max:
                self._drop(len(self._buf) - self.prefill)
                # The rest of the burst may still be arriving; judge the level from here.
                self._low, self._ticks = None, 0
            return True

    def active(self) -> bool:
        """A phone is live, or the end of its talk is still playing out."""
        with self._lock:
            return self._talking or self._playing or bool(self._buf)

    def take(self) -> bytes:
        """One tick of audio for ffmpeg: the oldest waiting audio, padded with silence."""
        with self._lock:
            if not self._playing:
                if len(self._buf) < (self.prefill if self._talking else 1):
                    if self._talking and self._heard:
                        self.gap_bytes += TICK_BYTES
                    return SILENCE
                self._playing = True
                self._heard = self._talking
                self._low, self._ticks = None, 0

            out = bytes(self._buf[:TICK_BYTES])
            del self._buf[:TICK_BYTES]
            if len(out) < TICK_BYTES:  # ran dry: fill with silence, then collect PREFILL again
                if self._talking:
                    self.gap_bytes += TICK_BYTES - len(out)
                self._playing = False
                return out + SILENCE[len(out):]

            # If the buffer never got near empty lately, the extra is just delay: trim it.
            rest = len(self._buf)
            self._low = rest if self._low is None else min(self._low, rest)
            self._ticks += 1
            if self._ticks >= self.trim_every:
                if self._low > self.slack:
                    self._drop(self._low - self.slack)
                self._low, self._ticks = None, 0
            return out

    def _drop(self, n: int):
        n &= ~1
        del self._buf[:n]
        self.dropped_bytes += n


class QueueFull(Exception):
    pass


class Announcement:
    def __init__(self, id_: int, client: str, pcm: bytes):
        self.id = id_
        self.client = client
        self.pcm = pcm[:len(pcm) & ~1]
        self.pos = 0

    @property
    def audio_s(self) -> float:
        return round(len(self.pcm) / 2 / RATE, 1)

    @property
    def played_s(self) -> float:
        return round(self.pos / 2 / RATE, 1)


class Announcements:
    """Recorded announcements, played whole and in the order they arrived.

    The feeder takes one tick at a time from the one playing. A live phone always comes
    first: while one holds the floor (hold()), the next announcement waits; while an
    announcement plays, hold() fails, so a phone can't cut into the middle of one.
    """

    def __init__(self, max_queued=MAX_QUEUED):
        self.max_queued = max_queued
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._current = None
        self._held = False
        self._ids = itertools.count(1)
        self._finished = collections.OrderedDict()  # recent id -> how it ended, for the page

    def add(self, client: str, pcm: bytes) -> tuple:
        """Queue an announcement. Returns (it, how many play before it). Raises QueueFull."""
        with self._lock:
            if len(self._queue) >= self.max_queued:
                raise QueueFull()
            a = Announcement(next(self._ids), client, pcm)
            ahead = len(self._queue) + (self._current is not None)
            self._queue.append(a)
        events.emit("announce_queued", id=a.id, client=client, audio_s=a.audio_s, ahead=ahead)
        return a, ahead

    def hold(self) -> bool:
        """A phone wants to go live: keep the next announcement waiting. False if one is playing."""
        with self._lock:
            if self._current is not None:
                return False
            self._held = True
            return True

    def release(self):
        with self._lock:
            self._held = False

    def take(self):
        """The next tick of the playing announcement, starting the next one if it's time.
        None when there's nothing to play."""
        started = finished = None
        with self._lock:
            a = self._current
            if a is None:
                if self._held or not self._queue:
                    return None
                a = started = self._current = self._queue.popleft()
            out = a.pcm[a.pos:a.pos + TICK_BYTES]
            a.pos += len(out)
            if a.pos >= len(a.pcm):
                finished, self._current = a, None
                self._ended(a, "played")
        if started:
            events.emit("announce_start", id=a.id, client=a.client, audio_s=a.audio_s)
        if finished:
            events.emit("announce_stop", id=a.id, client=a.client, reason="played",
                        audio_s=a.audio_s, played_s=a.played_s)
        return out + SILENCE[len(out):] if out else None

    def stop_all(self, reason: str, by: str = None) -> int:
        """Stop the playing announcement and drop the queued ones. Returns how many."""
        with self._lock:
            gone = ([self._current] if self._current else []) + list(self._queue)
            self._current = None
            self._queue.clear()
            for a in gone:
                self._ended(a, reason)
        for a in gone:
            events.emit("announce_stop", id=a.id, client=a.client, reason=reason, by=by,
                        audio_s=a.audio_s, played_s=a.played_s)
        return len(gone)

    def _ended(self, a: Announcement, how: str):
        self._finished[a.id] = how
        while len(self._finished) > 20:
            self._finished.popitem(last=False)

    def status(self) -> dict:
        with self._lock:
            a = self._current
            return {
                "playing": a and {"id": a.id, "audio_s": a.audio_s, "played_s": a.played_s},
                "queued": [q.id for q in self._queue],
                "held": self._held,  # a phone is live, so the next one waits
                "finished": {str(k): v for k, v in self._finished.items()},
            }

    def __len__(self):
        with self._lock:
            return len(self._queue) + (self._current is not None)


class Stream:
    """Owns the feeder thread and, through it, the one ffmpeg."""

    def __init__(self):
        self.buffer = JitterBuffer()
        self.announcements = Announcements()
        self._thread = None
        self._wake = threading.Event()
        self._stopping = False
        self._restart = False
        self._relay = None
        self.cfg = None

    # ---- control, from any thread ------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stopping = self._restart = False
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name="stream-feeder", daemon=True)
        self._thread.start()
        return self

    def restart(self):
        """Pick up new settings: stop ffmpeg and start it again. Returns at once."""
        self._restart = True
        self._wake.set()

    def stop(self):
        self._stopping = True
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(STOP_TIMEOUT_S)

    def state(self) -> str:
        """"up" (streaming), "off" (no destination saved yet) or "down" (ffmpeg failing)."""
        r = self._relay
        if r is not None and r.alive:
            return "up"
        return "off" if not settings.is_configured() else "down"

    @property
    def pid(self):
        r = self._relay
        return r.pid if r else None

    # ---- the feeder thread ---------------------------------------------------------

    def _run(self):
        backoff = RESTART_MIN_S
        while not self._stopping:
            self._restart = False
            self._wake.clear()
            if not settings.is_configured():
                self._wake.wait()  # until settings are saved, or we're stopped
                continue
            cfg = self.cfg = settings.load()
            r = relay.FFmpegRelay(cfg)
            started = None
            reason = "error"
            try:
                r.start()
                started = time.monotonic()
                self._relay = r
                events.emit("stream_start", codec=cfg["codec"],
                            dest=f"{cfg['ip']}:{cfg['port']}", ffmpeg_pid=r.pid)
                self._feed(r)
                reason = "shutdown" if self._stopping else "settings"
            except relay.RelayError as e:
                if not self._stopping:
                    events.emit("stream_error", level=logging.ERROR, error=str(e),
                                retry_in_s=backoff)
            except Exception as e:  # the stream must outlive any one bad ffmpeg
                log.exception("stream feeder")
                events.emit("stream_error", level=logging.ERROR, error=repr(e),
                            retry_in_s=backoff)
            finally:
                r.stop()
                self._relay = None
                if started is not None:
                    events.emit("stream_stop", reason=reason, ffmpeg_exit=r.returncode,
                                uptime_s=round(time.monotonic() - started, 1))
            if reason == "error" and not self._stopping:
                if started is not None and time.monotonic() - started > 60:
                    backoff = RESTART_MIN_S  # it ran fine for a while; start the backoff again
                self._wake.wait(backoff)
                backoff = min(backoff * 2, RESTART_MAX_S)
            else:
                backoff = RESTART_MIN_S

    def _feed(self, r):
        """Write one tick to ffmpeg every TICK_S until told to stop or restart."""
        t0 = time.monotonic()
        n = 0
        while not (self._stopping or self._restart):
            delay = t0 + n * TICK_S - time.monotonic()
            if delay > 0:
                self._wake.wait(delay)
                continue
            if -delay > RESYNC_AFTER_S:
                # The server stalled (overloaded, suspended). Catching up would send the
                # Core a burst it would then play late; skip the missed time instead.
                log.warning("stream clock fell %.0f ms behind; resynced", -delay * 1000)
                t0, n = time.monotonic(), 0
            r.write(self.next_tick())
            n += 1

    def next_tick(self) -> bytes:
        """The live phone if there is one, else the playing announcement, else silence."""
        if self.buffer.active():
            return self.buffer.take()
        return self.announcements.take() or SILENCE
