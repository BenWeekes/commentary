"""Realtime PCM pacing for relay_publish stdin.

Providers can return audio in bursts. relay_publish expects 16 kHz mono s16le
PCM at realtime cadence, so this writer buffers producer bytes and drains them
as 10 ms chunks.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field

from lib.constants import BYTES_PER_10MS, SAMPLE_RATE


PCM_FORMAT = "pcm_s16le"
PCM_CHANNELS = 1
PCM_SAMPLE_RATE = SAMPLE_RATE
PCM_BYTES_PER_10MS = BYTES_PER_10MS


@dataclass
class PacedPipeWriterStats:
    chunks_written: int = 0
    bytes_written: int = 0
    underruns: int = 0
    first_audio_at: float | None = None
    first_write_at: float | None = None
    last_write_at: float | None = None
    last_error: str = ""
    metadata: dict = field(default_factory=dict)


class PacedPipeWriter:
    """Drain 16 kHz mono s16le PCM into a pipe at 10 ms cadence."""

    def __init__(
        self,
        audio_pipe,
        *,
        stop_event: threading.Event | None = None,
        play_at: float | None = None,
        on_telemetry=None,
        source: str = "v2v",
        silence_on_underrun: bool = False,
    ):
        self.audio_pipe = audio_pipe
        self.stop_event = stop_event or threading.Event()
        self.play_at = play_at
        self.on_telemetry = on_telemetry
        self.source = source
        self.silence_on_underrun = silence_on_underrun
        self.stats = PacedPipeWriterStats()
        self._buf = collections.deque()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def write(self, pcm_bytes: bytes, metadata: dict | None = None):
        if not pcm_bytes or self._closed.is_set():
            return
        if len(pcm_bytes) % 2:
            pcm_bytes = pcm_bytes[:-1]
        if not pcm_bytes:
            return
        now = time.time()
        with self._lock:
            if self.stats.first_audio_at is None:
                self.stats.first_audio_at = now
            if metadata:
                self.stats.metadata.update(metadata)
            for idx in range(0, len(pcm_bytes), PCM_BYTES_PER_10MS):
                chunk = pcm_bytes[idx:idx + PCM_BYTES_PER_10MS]
                if len(chunk) < PCM_BYTES_PER_10MS:
                    chunk = chunk + (b"\x00" * (PCM_BYTES_PER_10MS - len(chunk)))
                self._buf.append(chunk)
        self._ready.set()

    def close(self, timeout: float = 2.0):
        self._closed.set()
        self._ready.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def buffered_ms(self) -> int:
        with self._lock:
            return len(self._buf) * 10

    def _run(self):
        if self.play_at is not None:
            while not self.stop_event.is_set() and not self._closed.is_set():
                remaining = self.play_at - time.time()
                if remaining <= 0:
                    break
                self._ready.wait(timeout=min(remaining, 0.05))

        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            with self._lock:
                chunk = self._buf.popleft() if self._buf else None
            if chunk is None:
                if self._closed.is_set():
                    break
                self.stats.underruns += 1
                if self.silence_on_underrun:
                    chunk = b"\x00" * PCM_BYTES_PER_10MS
                else:
                    self._ready.wait(timeout=0.05)
                    self._ready.clear()
                    next_tick = time.monotonic()
                    continue

            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
            try:
                self.audio_pipe.write(chunk)
                self.audio_pipe.flush()
            except (BrokenPipeError, OSError) as e:
                self.stats.last_error = str(e)
                self._closed.set()
                break

            wall = time.time()
            if self.stats.first_write_at is None:
                self.stats.first_write_at = wall
            self.stats.last_write_at = wall
            self.stats.chunks_written += 1
            self.stats.bytes_written += len(chunk)
            next_tick = max(next_tick + 0.01, time.monotonic())

        self._emit_telemetry()

    def _emit_telemetry(self):
        if not self.on_telemetry:
            return
        try:
            self.on_telemetry({
                "source": self.source,
                "status": "played" if self.stats.chunks_written else "dropped",
                "play_started_at": self.stats.first_write_at,
                "play_ended_at": self.stats.last_write_at,
                "actual_play_duration_ms": self.stats.chunks_written * 10,
                "total_buffered_ms": self.stats.chunks_written * 10,
                "interrupted": False,
                "interrupted_by": "",
                "v2v_first_audio_ms": (
                    round((self.stats.first_audio_at - self.play_at) * 1000)
                    if self.play_at is not None and self.stats.first_audio_at is not None
                    else None
                ),
                "v2v_total_audio_ms": self.stats.chunks_written * 10,
                "v2v_underruns": self.stats.underruns,
                "error": self.stats.last_error,
                **self.stats.metadata,
            })
        except Exception:
            pass
