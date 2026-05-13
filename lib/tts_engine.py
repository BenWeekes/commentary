import asyncio
import base64
import collections
import heapq
import json
import os
import queue
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import websockets

from lib.constants import SAMPLE_RATE, BYTES_PER_10MS, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL


def _ts(video_start=None):
    """Current clock time for log stamps, with optional video-relative time."""
    wall = time.strftime("%H:%M:%S")
    if video_start is not None:
        vt = time.time() - video_start
        m, s = divmod(vt, 60)
        return f"{wall} V+{int(m):02d}:{s:05.2f}"
    return wall


class TTSEngine:
    """
    ElevenLabs WebSocket TTS → PCM → Go publisher stdin.

    Architecture:
      - TTS worker processes utterances from the text queue with lookahead:
        while the current utterance plays, the next one is translated + TTS'd
        in parallel so playback time doesn't eat into the next item's budget.
      - Each utterance is fully buffered from ElevenLabs before playback begins
        (pre-buffer) to avoid underruns from network jitter.
      - Pipe-writer drains the buffer at a steady 10ms rate to the Go publisher.
      - No silence is ever sent — Go publisher handles silence on its own.
      - speak(interrupt=True) cuts active/queued playback so fresher commentary
        can take over.
    """

    def __init__(self, audio_pipe, voice_id=ELEVENLABS_VOICE_ID,
                 model=ELEVENLABS_MODEL, api_key=None, on_telemetry=None):
        self.audio_pipe = audio_pipe
        self.voice_id = voice_id
        self.model = model
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.on_telemetry = on_telemetry  # callable(dict) with timing data
        self._stop = threading.Event()
        self._closing = False  # phase 1: reject new work, let in-flight drain
        self._interrupt = threading.Event()
        self._pipe_writer_thread = None
        self._tts_worker_thread = None
        # Thread-safe audio buffer: TTS pushes bytes, pipe-writer drains
        self._audio_buf = collections.deque()
        self._buf_lock = threading.Lock()
        # Signals that a complete utterance is buffered and ready for playback
        self._playback_ready = threading.Event()
        # Text queue for non-blocking speak()
        self._text_queue = queue.Queue()
        self._loop = None
        # Callback when TTS finishes an utterance (called from worker thread)
        self.on_idle = None  # set externally: callable()
        # Track speaking state
        self.is_speaking = threading.Event()
        # SR (Sportradar) separate audio buffer — fed by SRPrefetcher
        self._sr_audio_buf = collections.deque()
        self._sr_buf_lock = threading.Lock()
        self._sr_playback_ready = threading.Event()
        # Wakes pipe writer for either STT or SR audio
        self._any_playback_ready = threading.Event()
        # When set, TTS worker discards current utterance output (SR GOAL playing)
        self._stt_suppressed = threading.Event()
        # Atmosphere mixing
        self._atmosphere_pcm = None   # raw PCM bytes (entire file)
        self._atmosphere_pos = 0      # current read position
        self._atmosphere_on = False   # toggle
        self._atmosphere_vol = 0.5    # mix volume (half of Mel-Band Roformer output)
        self._atmosphere_lock = threading.Lock()
        # Original audio (pass-through of source commentary)
        self._original_pcm = None        # raw PCM bytes (entire file, 16kHz mono)
        self._original_pos = 0           # current read position
        self._original_on = False        # toggle
        self._original_lock = threading.Lock()
        # Lookahead: pre-translated + pre-TTS'd next utterance (overlaps with playback)
        self._lookahead_buf = collections.deque()
        self._lookahead_lock = threading.Lock()
        self._lookahead_item = None  # result dict from _process_item
        self._lang_version = 0  # bumped on language change to invalidate in-flight lookahead
        # Redirect target for _push_audio — normally _audio_buf, switched to _lookahead_buf during lookahead
        self._tts_target_buf = None  # set dynamically in worker
        self._tts_target_lock = None
        # Metadata slots for telemetry enrichment (single-slot, not FIFO)
        self._playback_meta_slot = None    # protected by _buf_lock
        self._sr_playback_meta_slot = None  # protected by _sr_buf_lock
        self._skipped_meta = collections.deque()     # single-producer/single-consumer, no lock needed
        # Next STT play_at — set by tts_worker during hold-sleep, read by pipe_writer
        # to avoid starting SR playback that will be interrupted by imminent STT.
        # Single float, no lock needed (CPython GIL makes float assignment atomic).
        self._next_stt_play_at = None
        # Pre-translation: parallel executor translates queued items ahead of TTS worker
        self._pretranslated = collections.OrderedDict()  # (text, play_at) → {translated, voice_id, translate_time}
        self._pretranslate_lock = threading.Lock()
        self._pretranslate_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pretranslate")
        self._prepare_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts-prepare")
        self._prepare_done = queue.Queue()
        self._ready_heap = []
        self._ready_lock = threading.Lock()
        self._ready_seq = 0
        self._inflight_play_ats = set()
        self._inflight_lock = threading.Lock()
        self._uid_lock = threading.Lock()
        # Stats
        self._utterance_id = 0
        self._elevenlabs_speed = 1.0
        self._elevenlabs_stability = 1.0
        self._elevenlabs_similarity_boost = 1.0
        self._min_local_speed = 1.0 / 1.3
        self._max_local_speed = 1.30
        self._fit_guard_s = 0.05
        self._late_start_grace_s = 0.05
        # Video-relative timestamp (set by pipeline after publisher starts)
        self.video_start = None

    def _vts(self):
        """Timestamp with video-relative time if available."""
        return _ts(self.video_start)

    def start(self):
        """Start pipe-writer and TTS worker threads."""
        self._pipe_writer_thread = threading.Thread(target=self._pipe_writer, daemon=True)
        self._pipe_writer_thread.start()
        self._tts_worker_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._tts_worker_thread.start()

    def _pipe_writer(self):
        """
        Drains audio buffers at 10ms rate. Checks both STT (_audio_buf) and
        SR (_sr_audio_buf). STT has priority: if STT audio becomes ready while
        SR is playing, SR is interrupted. SR never interrupts STT.

        When atmosphere is enabled and no TTS/SR audio is playing, writes
        atmosphere-only chunks at 10ms rate to keep crowd noise continuous.
        """
        silence = b'\x00' * BYTES_PER_10MS
        atmos_tick = time.monotonic()

        while not self._stop.is_set():
            # Drain skipped (dropped/suppressed) items — emit telemetry promptly
            while self._skipped_meta:
                skipped = self._skipped_meta.popleft()
                if self.on_telemetry:
                    try:
                        self.on_telemetry(skipped)
                    except Exception:
                        pass

            # Block until either source has audio ready, or write atmosphere/original
            while not self._stop.is_set():
                # Drain skipped items inside inner loop too (original-audio mode
                # keeps this loop running without breaking to the outer drain)
                while self._skipped_meta:
                    skipped = self._skipped_meta.popleft()
                    if self.on_telemetry:
                        try:
                            self.on_telemetry(skipped)
                        except Exception:
                            pass

                # Original audio mode — write original chunks continuously
                if self._original_on and self._original_pcm:
                    now = time.monotonic()
                    if now >= atmos_tick:
                        chunk = self._get_original_chunk()
                        if chunk:
                            try:
                                self.audio_pipe.write(chunk)
                                self.audio_pipe.flush()
                            except (BrokenPipeError, OSError):
                                self._stop.set()
                                break
                            atmos_tick = now + 0.01
                        else:
                            time.sleep(0.005)  # past end of audio
                    continue  # skip TTS/SR check

                # Use short timeout to check for TTS/SR readiness
                if self._any_playback_ready.wait(timeout=0.005):
                    break
                # No TTS/SR ready — write atmosphere at steady 10ms rate
                if self._atmosphere_on and self._atmosphere_pcm:
                    now = time.monotonic()
                    if now >= atmos_tick:
                        chunk = self._mix_atmosphere_chunk(silence)
                        try:
                            self.audio_pipe.write(chunk)
                            self.audio_pipe.flush()
                        except (BrokenPipeError, OSError):
                            self._stop.set()
                            break
                        atmos_tick = now + 0.01
            if self._stop.is_set():
                break

            self._any_playback_ready.clear()

            # Determine source: STT has priority
            if self._playback_ready.is_set():
                source = "STT"
                self._playback_ready.clear()
                buf = self._audio_buf
                lock = self._buf_lock
            elif self._sr_playback_ready.is_set():
                source = "SR"
                self._sr_playback_ready.clear()
                buf = self._sr_audio_buf
                lock = self._sr_buf_lock
            else:
                continue

            with lock:
                n_chunks = len(buf)
                if source == "STT":
                    current_meta = self._playback_meta_slot
                    self._playback_meta_slot = None
                else:
                    current_meta = self._sr_playback_meta_slot
                    self._sr_playback_meta_slot = None
            if n_chunks == 0:
                # Drain any orphaned metadata (e.g. from empty-audio lookahead)
                meta = current_meta or {}
                if meta and self.on_telemetry:
                    try:
                        self.on_telemetry({
                            "source": source.lower(),
                            "status": "dropped",
                            "play_started_at": None, "play_ended_at": None,
                            "actual_play_duration_ms": 0,
                            "total_buffered_ms": 0,
                            "interrupted": False, "interrupted_by": "",
                            "uid": meta.get("uid"),
                            "text": meta.get("text"),
                            "translated": meta.get("translated"),
                            "translate_time": meta.get("translate_time"),
                            "tts_time": meta.get("tts_time"),
                            "play_at": meta.get("play_at"),
                            "pre_translated": meta.get("pre_translated", False),
                            "queue_wait_ms": meta.get("queue_wait_ms", 0),
                            "translation_model_used": meta.get("translation_model_used"),
                            "translation_fallback_reason": meta.get("translation_fallback_reason"),
                        })
                    except Exception:
                        pass
                continue

            # Skip SR if STT is arriving before it would finish
            if source == "SR":
                meta = current_meta or {}
                play_at = meta.get("play_at")
                if play_at:
                    late_ms = (time.time() - play_at) * 1000
                    if late_ms > 50:
                        print(f"  [{self._vts()}] [PIPE] SR skipped — "
                              f"{late_ms:.0f}ms late (stale)")
                        with lock:
                            buf.clear()
                        if self.on_telemetry:
                            try:
                                self.on_telemetry({
                                    "source": "sr", "status": "dropped",
                                    "play_started_at": None, "play_ended_at": None,
                                    "actual_play_duration_ms": 0,
                                    "total_buffered_ms": n_chunks * 10,
                                    "interrupted": False, "interrupted_by": "stale",
                                    "uid": meta.get("uid"),
                                    "text": meta.get("text"),
                                    "translated": meta.get("translated"),
                                    "translate_time": meta.get("translate_time"),
                                    "tts_time": meta.get("tts_time"),
                                    "play_at": play_at,
                                    "pre_translated": meta.get("pre_translated", False),
                                    "queue_wait_ms": meta.get("queue_wait_ms", 0),
                                    "translation_model_used": meta.get("translation_model_used"),
                                    "translation_fallback_reason": meta.get("translation_fallback_reason"),
                                })
                            except Exception:
                                pass
                        continue

                stt_due = self._next_stt_play_at
                if stt_due:
                    sr_end = time.time() + n_chunks * 0.01
                    if stt_due < sr_end:
                        print(f"  [{self._vts()}] [PIPE] SR skipped — "
                              f"STT due in {stt_due - time.time():.2f}s, "
                              f"SR would take {n_chunks * 10}ms")
                        with lock:
                            buf.clear()
                        if meta and self.on_telemetry:
                            try:
                                self.on_telemetry({
                                    "source": "sr", "status": "dropped",
                                    "play_started_at": None, "play_ended_at": None,
                                    "actual_play_duration_ms": 0,
                                    "total_buffered_ms": n_chunks * 10,
                                    "interrupted": False, "interrupted_by": "stt_imminent",
                                    "uid": meta.get("uid"),
                                    "text": meta.get("text"),
                                    "translated": meta.get("translated"),
                                    "translate_time": meta.get("translate_time"),
                                    "tts_time": meta.get("tts_time"),
                                    "play_at": meta.get("play_at"),
                                    "pre_translated": meta.get("pre_translated", False),
                                    "queue_wait_ms": meta.get("queue_wait_ms", 0),
                                    "translation_model_used": meta.get("translation_model_used"),
                                    "translation_fallback_reason": meta.get("translation_fallback_reason"),
                                })
                            except Exception:
                                pass
                        continue

            print(f"  [{self._vts()}] [PIPE] {source} playback started — {n_chunks * 10}ms buffered")
            next_tick = time.monotonic()
            play_started_at = time.time()
            was_interrupted = False
            interrupted_by = ""
            chunks_played = 0

            while (
                not self._stop.is_set()
                and not self._interrupt.is_set()
                and chunks_played < n_chunks
            ):
                # During SR playback, check if STT has become ready — interrupt SR
                if source == "SR" and self._playback_ready.is_set():
                    with self._sr_buf_lock:
                        self._sr_audio_buf.clear()
                    print(f"  [{self._vts()}] [PIPE] SR interrupted by STT")
                    was_interrupted = True
                    interrupted_by = "stt"
                    # Don't clear _any_playback_ready — STT needs it
                    break

                chunk = None
                with lock:
                    if buf:
                        chunk = buf.popleft()

                if not chunk:
                    break  # utterance done

                # Mix atmosphere into TTS/SR audio
                if self._atmosphere_on and self._atmosphere_pcm:
                    chunk = self._mix_atmosphere_chunk(chunk)

                try:
                    self.audio_pipe.write(chunk)
                    self.audio_pipe.flush()
                except (BrokenPipeError, OSError):
                    print(f"  [{self._vts()}] [PIPE] Pipe closed")
                    self._stop.set()
                    break

                chunks_played += 1
                next_tick += 0.01
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)

            play_ended_at = time.time()
            print(f"  [{self._vts()}] [PIPE] {source} playback ended")

            # Detect interrupt-caused breakout. For STT this is newer STT
            # taking over; for SR it is also STT preemption before the queued
            # STT audio has finished buffering.
            if self._interrupt.is_set() and not was_interrupted:
                was_interrupted = True
                interrupted_by = "stt_interrupt"

            # Use metadata captured at playback start (not post-playback pop)
            meta = current_meta or {}
            if was_interrupted:
                with lock:
                    buf.clear()

            if self.on_telemetry:
                try:
                    self.on_telemetry({
                        "source": source.lower(),
                        "status": "interrupted" if was_interrupted else "played",
                        "play_started_at": play_started_at,
                        "play_ended_at": play_ended_at,
                        "actual_play_duration_ms": chunks_played * 10,
                        "total_buffered_ms": n_chunks * 10,
                        "interrupted": was_interrupted,
                        "interrupted_by": interrupted_by,
                        "uid": meta.get("uid"),
                        "text": meta.get("text"),
                        "translated": meta.get("translated"),
                        "translate_time": meta.get("translate_time"),
                        "tts_time": meta.get("tts_time"),
                        "play_at": meta.get("play_at"),
                        "pre_translated": meta.get("pre_translated", False),
                        "queue_wait_ms": meta.get("queue_wait_ms", 0),
                        "translation_model_used": meta.get("translation_model_used"),
                        "translation_fallback_reason": meta.get("translation_fallback_reason"),
                        "local_speed_factor": meta.get("local_speed_factor"),
                        "fit_from_ms": meta.get("fit_from_ms"),
                        "fit_to_ms": meta.get("fit_to_ms"),
                        "fit_deadline_ms": meta.get("fit_deadline_ms"),
                        "fit_cpu_ms": meta.get("fit_cpu_ms"),
                        "fit_reason": meta.get("fit_reason"),
                        "voice_id": meta.get("voice_id"),
                        "prepare_started_at": meta.get("prepare_started_at"),
                        "translate_started_at": meta.get("translate_started_at"),
                        "translate_ended_at": meta.get("translate_ended_at"),
                        "tts_started_at": meta.get("tts_started_at"),
                        "tts_ended_at": meta.get("tts_ended_at"),
                        "ready_at": meta.get("ready_at"),
                    })
                except Exception:
                    pass
            atmos_tick = time.monotonic()  # reset so idle atmosphere resumes cleanly

            # If SR was interrupted by STT, loop back — _any_playback_ready
            # is still set from the STT _playback_ready.set() call
            if source == "SR" and self._playback_ready.is_set():
                self._any_playback_ready.set()

        # Final drain on shutdown — emit telemetry for any remaining items
        while self._skipped_meta:
            skipped = self._skipped_meta.popleft()
            if self.on_telemetry:
                try:
                    self.on_telemetry(skipped)
                except Exception:
                    pass
        with self._buf_lock:
            meta = self._playback_meta_slot
            self._playback_meta_slot = None
        if meta and self.on_telemetry:
            try:
                self.on_telemetry({
                    "source": "stt", "status": "dropped",
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                    "interrupted": False, "interrupted_by": "shutdown",
                    "uid": meta.get("uid"), "text": meta.get("text"),
                    "translated": meta.get("translated"),
                    "translate_time": meta.get("translate_time"),
                    "tts_time": meta.get("tts_time"),
                    "play_at": meta.get("play_at"),
                    "pre_translated": meta.get("pre_translated", False),
                    "queue_wait_ms": meta.get("queue_wait_ms", 0),
                    "translation_model_used": meta.get("translation_model_used"),
                    "translation_fallback_reason": meta.get("translation_fallback_reason"),
                })
            except Exception:
                pass
        with self._sr_buf_lock:
            meta = self._sr_playback_meta_slot
            self._sr_playback_meta_slot = None
        if meta and self.on_telemetry:
            try:
                self.on_telemetry({
                    "source": "sr", "status": "dropped",
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                    "interrupted": False, "interrupted_by": "shutdown",
                    "uid": meta.get("uid"), "text": meta.get("text"),
                    "translated": meta.get("translated"),
                    "translate_time": meta.get("translate_time"),
                    "tts_time": meta.get("tts_time"),
                    "play_at": meta.get("play_at"),
                    "pre_translated": meta.get("pre_translated", False),
                    "queue_wait_ms": meta.get("queue_wait_ms", 0),
                })
            except Exception:
                pass

    def _push_audio(self, pcm_bytes):
        """Split PCM bytes into 10ms chunks and push to target buffer.
        Target defaults to _audio_buf but switches to _lookahead_buf during lookahead."""
        buf = self._audio_buf if self._tts_target_buf is None else self._tts_target_buf
        lock = self._buf_lock if self._tts_target_lock is None else self._tts_target_lock
        with lock:
            if self._interrupt.is_set():
                return
            offset = 0
            while offset < len(pcm_bytes):
                end = offset + BYTES_PER_10MS
                chunk = pcm_bytes[offset:end]
                if len(chunk) < BYTES_PER_10MS:
                    chunk = chunk + b'\x00' * (BYTES_PER_10MS - len(chunk))
                buf.append(chunk)
                offset = end

    def _current_audio_bytes(self):
        with self._buf_lock:
            return b"".join(self._audio_buf)

    def _replace_current_audio(self, pcm_bytes):
        chunks = []
        offset = 0
        while offset < len(pcm_bytes):
            end = offset + BYTES_PER_10MS
            chunk = pcm_bytes[offset:end]
            if len(chunk) < BYTES_PER_10MS:
                chunk = chunk + b"\x00" * (BYTES_PER_10MS - len(chunk))
            chunks.append(chunk)
            offset = end

        with self._buf_lock:
            self._audio_buf.clear()
            self._audio_buf.extend(chunks)

    def _current_audio_ms(self):
        with self._buf_lock:
            return len(self._audio_buf) * 10

    def _next_queued_play_at(self, current_play_at):
        deadlines = []
        if self._lookahead_item:
            la_play_at = self._lookahead_item.get("play_at")
            if la_play_at and la_play_at > current_play_at:
                deadlines.append(la_play_at)

        with self._text_queue.mutex:
            queued_items = list(self._text_queue.queue)
        for item in queued_items:
            if not isinstance(item, tuple):
                continue
            item_play_at = item[1]
            if item_play_at and item_play_at > current_play_at:
                deadlines.append(item_play_at)

        with self._ready_lock:
            for _, _, result in self._ready_heap:
                item_play_at = result.get("play_at")
                if item_play_at and item_play_at > current_play_at:
                    deadlines.append(item_play_at)

        with self._inflight_lock:
            for item_play_at in self._inflight_play_ats:
                if item_play_at and item_play_at > current_play_at:
                    deadlines.append(item_play_at)

        return min(deadlines) if deadlines else None

    def _atempo_filter(self, factor):
        parts = []
        remaining = factor
        while remaining < 0.5:
            parts.append("atempo=0.5")
            remaining /= 0.5
        while remaining > 2.0:
            parts.append("atempo=2.0")
            remaining /= 2.0
        parts.append(f"atempo={remaining:.6f}")
        return ",".join(parts)

    def _tempo_pcm(self, pcm_bytes, factor):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-af", self._atempo_filter(factor),
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
        ]
        return subprocess.check_output(cmd, input=pcm_bytes)

    def _item_fields(self, item):
        if not isinstance(item, tuple):
            return item, None, None, None, None
        if len(item) == 5:
            return item
        if len(item) == 4:
            text, play_at, translate_fn, enqueued_at = item
            return text, play_at, translate_fn, enqueued_at, None
        text = item[0] if len(item) > 0 else None
        play_at = item[1] if len(item) > 1 else None
        translate_fn = item[2] if len(item) > 2 else None
        enqueued_at = item[3] if len(item) > 3 else None
        target_duration_s = item[4] if len(item) > 4 else None
        return text, play_at, translate_fn, enqueued_at, target_duration_s

    def _unpack_translate_result(self, result):
        if isinstance(result, tuple):
            if len(result) >= 3:
                return result[0], result[1], result[2]
            return result[0], result[1], None
        return result, self.voice_id, None

    def _fit_current_audio_to_target_duration(self, result):
        # Provider word spans are not consistently a good natural speech
        # duration target. Pacing is based on the next scheduled STT gap when
        # that gap is known; otherwise we keep the generated duration.
        return

    def _fit_current_audio_to_next_play_at(self, result):
        play_at = result.get("play_at")
        if not play_at:
            return

        deadline = self._next_queued_play_at(play_at)
        if not deadline:
            return

        available_s = deadline - play_at - self._fit_guard_s
        if available_s <= 0:
            return

        pcm_bytes = self._current_audio_bytes()
        if not pcm_bytes:
            return

        current_s = len(pcm_bytes) / (SAMPLE_RATE * 2)
        needed = current_s / available_s
        if 0.95 <= needed <= 1.05:
            return

        factor = max(self._min_local_speed, min(needed, self._max_local_speed))
        try:
            started = time.monotonic()
            fitted = self._tempo_pcm(pcm_bytes, factor)
            elapsed_ms = (time.monotonic() - started) * 1000
        except Exception as e:
            print(f"  [{self._vts()}] [TTS #{result.get('uid')}] speed-fit failed: {e}")
            return

        self._replace_current_audio(fitted)
        fitted_s = len(fitted) / (SAMPLE_RATE * 2)
        result["local_speed_factor"] = factor
        result["fit_from_ms"] = round(current_s * 1000)
        result["fit_to_ms"] = round(fitted_s * 1000)
        result["fit_deadline_ms"] = round(available_s * 1000)
        result["fit_cpu_ms"] = round(elapsed_ms)
        result["fit_reason"] = "next_play_at"
        capped = ""
        if needed > self._max_local_speed:
            capped = " capped_fast"
        elif needed < self._min_local_speed:
            capped = " capped_slow"
        print(f"  [{self._vts()}] [TTS #{result.get('uid')}] Gap-fit {current_s:.2f}s → "
              f"{fitted_s:.2f}s for {available_s:.2f}s window "
              f"(factor={factor:.2f}x{capped}, atempo={elapsed_ms:.0f}ms)")

    def _audio_bytes_to_chunks(self, pcm_bytes):
        chunks = []
        offset = 0
        while offset < len(pcm_bytes):
            end = offset + BYTES_PER_10MS
            chunk = pcm_bytes[offset:end]
            if len(chunk) < BYTES_PER_10MS:
                chunk = chunk + b"\x00" * (BYTES_PER_10MS - len(chunk))
            chunks.append(chunk)
            offset = end
        return chunks

    def _fit_result_audio_to_next_play_at(self, result):
        play_at = result.get("play_at")
        pcm_bytes = result.get("pcm_bytes") or b""
        if not play_at or not pcm_bytes:
            return

        deadline = self._next_queued_play_at(play_at)
        if not deadline:
            return

        available_s = deadline - play_at - self._fit_guard_s
        if available_s <= 0:
            return

        current_s = len(pcm_bytes) / (SAMPLE_RATE * 2)
        needed = current_s / available_s
        if 0.95 <= needed <= 1.05:
            return

        factor = max(self._min_local_speed, min(needed, self._max_local_speed))
        try:
            started = time.monotonic()
            fitted = self._tempo_pcm(pcm_bytes, factor)
            elapsed_ms = (time.monotonic() - started) * 1000
        except Exception as e:
            print(f"  [{self._vts()}] [TTS #{result.get('uid')}] speed-fit failed: {e}")
            return

        fitted_s = len(fitted) / (SAMPLE_RATE * 2)
        result["pcm_bytes"] = fitted
        result["local_speed_factor"] = factor
        result["fit_from_ms"] = round(current_s * 1000)
        result["fit_to_ms"] = round(fitted_s * 1000)
        result["fit_deadline_ms"] = round(available_s * 1000)
        result["fit_cpu_ms"] = round(elapsed_ms)
        result["fit_reason"] = "next_play_at"
        capped = ""
        if needed > self._max_local_speed:
            capped = " capped_fast"
        elif needed < self._min_local_speed:
            capped = " capped_slow"
        print(f"  [{self._vts()}] [TTS #{result.get('uid')}] Gap-fit {current_s:.2f}s → "
              f"{fitted_s:.2f}s for {available_s:.2f}s window "
              f"(factor={factor:.2f}x{capped}, atempo={elapsed_ms:.0f}ms)")

    def speak(self, text, interrupt=False, play_at=None, translate_fn=None, target_duration_s=None):
        """
        Non-blocking. Queues text for sequential TTS playback.
        text: English text to speak (will be translated just-in-time if translate_fn set)
        play_at: absolute time.time() when playback should start (pre-fetch + hold)
        translate_fn: callable(text) -> translated_text (uses current lang at TTS time)
        """
        if self._closing:
            return
        if interrupt:
            self._lang_version += 1
            self._interrupt.set()
            self._stt_suppressed.clear()
            with self._buf_lock:
                pending_meta = self._playback_meta_slot
                pending_ms = len(self._audio_buf) * 10
                self._audio_buf.clear()
                self._playback_meta_slot = None
            if pending_meta:
                self._skipped_meta.append({
                    "source": "stt", "status": "replaced",
                    "uid": pending_meta.get("uid"), "text": pending_meta.get("text"),
                    "translated": pending_meta.get("translated"),
                    "translate_time": pending_meta.get("translate_time"),
                    "tts_time": pending_meta.get("tts_time"),
                    "play_at": pending_meta.get("play_at"),
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": pending_ms,
                    "interrupted": False, "interrupted_by": "stt_interrupt",
                    "pre_translated": pending_meta.get("pre_translated", False),
                    "queue_wait_ms": pending_meta.get("queue_wait_ms", 0),
                    "local_speed_factor": pending_meta.get("local_speed_factor"),
                    "fit_from_ms": pending_meta.get("fit_from_ms"),
                    "fit_to_ms": pending_meta.get("fit_to_ms"),
                    "fit_deadline_ms": pending_meta.get("fit_deadline_ms"),
                    "fit_cpu_ms": pending_meta.get("fit_cpu_ms"),
                })
            with self._sr_buf_lock:
                self._sr_audio_buf.clear()
                self._sr_playback_meta_slot = None
            with self._lookahead_lock:
                lookahead_item = self._lookahead_item
                lookahead_ms = len(self._lookahead_buf) * 10
                self._lookahead_buf.clear()
            with self._pretranslate_lock:
                self._pretranslated.clear()
            with self._ready_lock:
                while self._ready_heap:
                    _, _, ready_item = heapq.heappop(self._ready_heap)
                    self._emit_dropped_result(ready_item, status="replaced", interrupted_by="stt_interrupt")
            self._sr_playback_ready.clear()
            int_discarded = 0
            while not self._text_queue.empty():
                try:
                    stale_item = self._text_queue.get_nowait()
                    int_discarded += 1
                    stale_text, stale_play_at, _, _, _ = self._item_fields(stale_item)
                    self._skipped_meta.append({
                        "source": "stt", "status": "replaced",
                        "uid": None, "text": stale_text,
                        "translated": None,
                        "translate_time": None, "tts_time": None,
                        "play_at": stale_play_at,
                        "play_started_at": None, "play_ended_at": None,
                        "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                        "interrupted": False, "interrupted_by": "stt_interrupt",
                        "pre_translated": False, "queue_wait_ms": 0,
                    })
                except queue.Empty:
                    break
            # Also emit for lookahead item being discarded
            if lookahead_item:
                la = lookahead_item
                self._skipped_meta.append({
                    "source": "stt", "status": "replaced",
                    "uid": la.get("uid"), "text": la.get("text"),
                    "translated": la.get("translated"),
                    "translate_time": la.get("translate_time"),
                    "tts_time": la.get("tts_time"),
                    "play_at": la.get("play_at"),
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": lookahead_ms,
                    "interrupted": False, "interrupted_by": "stt_interrupt",
                    "pre_translated": la.get("pre_translated", False),
                    "queue_wait_ms": la.get("queue_wait_ms", 0),
                    "local_speed_factor": la.get("local_speed_factor"),
                    "fit_from_ms": la.get("fit_from_ms"),
                    "fit_to_ms": la.get("fit_to_ms"),
                    "fit_deadline_ms": la.get("fit_deadline_ms"),
                    "fit_cpu_ms": la.get("fit_cpu_ms"),
                })
                int_discarded += 1
            self._lookahead_item = None
            print(f"  [{self._vts()}] [TTS] Interrupted — STT+SR queues cleared"
                  f"{f' ({int_discarded} queued items discarded)' if int_discarded else ''}")
        if text:
            if play_at and not interrupt:
                # Only discard queued items whose play_at has already passed.
                # Items still in the future can be played on time — the TTS
                # worker will drop them later if they end up late.
                now = time.time()
                discarded = 0
                keep = []
                while not self._text_queue.empty():
                    try:
                        queued_item = self._text_queue.get_nowait()
                        _, item_play_at, _, _, _ = self._item_fields(queued_item)
                        if item_play_at and item_play_at > now:
                            keep.append(queued_item)
                        else:
                            discarded += 1
                            stale_text, stale_play_at, _, _, _ = self._item_fields(queued_item)
                            self._skipped_meta.append({
                                "source": "stt", "status": "replaced",
                                "uid": None, "text": stale_text,
                                "translated": None,
                                "translate_time": None, "tts_time": None,
                                "play_at": stale_play_at,
                                "play_started_at": None, "play_ended_at": None,
                                "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                                "interrupted": False, "interrupted_by": "",
                                "pre_translated": False, "queue_wait_ms": 0,
                            })
                    except queue.Empty:
                        break
                # Re-queue items that still have time
                for kept_item in keep:
                    self._text_queue.put(kept_item)
                # Only discard lookahead if its play_at has passed
                if self._lookahead_item:
                    la_play_at = self._lookahead_item.get("play_at")
                    if not la_play_at or la_play_at <= now:
                        la = self._lookahead_item
                        with self._lookahead_lock:
                            la_ms = len(self._lookahead_buf) * 10
                            self._lookahead_buf.clear()
                        self._lookahead_item = None
                        discarded += 1
                        self._skipped_meta.append({
                            "source": "stt", "status": "replaced",
                            "uid": la.get("uid"), "text": la.get("text"),
                            "translated": la.get("translated"),
                            "translate_time": la.get("translate_time"),
                            "tts_time": la.get("tts_time"),
                            "play_at": la.get("play_at"),
                            "play_started_at": None, "play_ended_at": None,
                            "actual_play_duration_ms": 0, "total_buffered_ms": la_ms,
                            "interrupted": False, "interrupted_by": "",
                            "pre_translated": la.get("pre_translated", False),
                            "queue_wait_ms": la.get("queue_wait_ms", 0),
                        })
                if discarded:
                    print(f"  [{self._vts()}] [TTS] Replaced {discarded} stale queued item(s)"
                          f"{f' (kept {len(keep)})' if keep else ''}")
            self._text_queue.put((text, play_at, translate_fn, time.time(), target_duration_s))

    def clear_stt(self):
        """
        Clear STT queue and audio without interrupting SR playback.
        Used when an SR INTERRUPT event (e.g. GOAL) is already playing
        in _sr_audio_buf and we want to prevent STT from preempting it.
        Sets _stt_suppressed so the TTS worker discards its in-flight
        utterance instead of signaling _playback_ready.
        """
        self._lang_version += 1
        self._stt_suppressed.set()
        with self._buf_lock:
            self._audio_buf.clear()
            self._playback_meta_slot = None
        with self._lookahead_lock:
            self._lookahead_buf.clear()
        # Emit telemetry for discarded lookahead
        if self._lookahead_item:
            la = self._lookahead_item
            self._skipped_meta.append({
                "source": "stt", "status": "suppressed",
                "uid": la.get("uid"), "text": la.get("text"),
                "translated": la.get("translated"),
                "translate_time": la.get("translate_time"),
                "tts_time": la.get("tts_time"),
                "play_at": la.get("play_at"),
                "play_started_at": None, "play_ended_at": None,
                "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                "interrupted": False, "interrupted_by": "clear_stt",
                "pre_translated": la.get("pre_translated", False),
                "queue_wait_ms": la.get("queue_wait_ms", 0),
            })
        self._lookahead_item = None
        with self._ready_lock:
            while self._ready_heap:
                _, _, ready_item = heapq.heappop(self._ready_heap)
                self._emit_dropped_result(ready_item, status="suppressed", interrupted_by="clear_stt")
        self._playback_ready.clear()
        stt_cleared = 0
        while not self._text_queue.empty():
            try:
                stale_item = self._text_queue.get_nowait()
                stt_cleared += 1
                stale_text, stale_play_at, _, _, _ = self._item_fields(stale_item)
                self._skipped_meta.append({
                    "source": "stt", "status": "suppressed",
                    "uid": None, "text": stale_text,
                    "translated": None,
                    "translate_time": None, "tts_time": None,
                    "play_at": stale_play_at,
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                    "interrupted": False, "interrupted_by": "clear_stt",
                    "pre_translated": False, "queue_wait_ms": 0,
                })
            except queue.Empty:
                break
        print(f"  [{self._vts()}] [TTS] STT cleared (SR playback preserved)"
              f"{f' ({stt_cleared} queued items suppressed)' if stt_cleared else ''}")

    def queue_size(self):
        with self._ready_lock:
            ready = len(self._ready_heap)
        with self._inflight_lock:
            inflight = len(self._inflight_play_ats)
        return self._text_queue.qsize() + ready + inflight

    def _pretranslate_queued(self):
        """Submit queued items for parallel pre-translation on the executor.
        Called from speak() after enqueuing a new item."""
        # Drain + re-queue to peek at all items
        items = []
        while not self._text_queue.empty():
            try:
                items.append(self._text_queue.get_nowait())
            except queue.Empty:
                break
        for it in items:
            self._text_queue.put(it)

        with self._pretranslate_lock:
            for it in items:
                if not isinstance(it, tuple):
                    continue
                text, play_at, translate_fn, _, _ = self._item_fields(it)
                if not translate_fn:
                    continue
                key = (text, play_at)
                if key in self._pretranslated:
                    continue
                # Submit translation to thread pool
                try:
                    self._pretranslate_executor.submit(
                        self._do_pretranslate, text, play_at, translate_fn
                    )
                except RuntimeError:
                    break  # executor shut down

    def _do_pretranslate(self, text, play_at, translate_fn):
        """Run translate_fn and cache the result (called on executor thread)."""
        t0 = time.monotonic()
        try:
            result = translate_fn(text)
            translated, voice_id, translation_meta = self._unpack_translate_result(result)
        except Exception:
            return  # translation failed — _process_item will do its own call
        translate_time = time.monotonic() - t0

        with self._pretranslate_lock:
            key = (text, play_at)
            self._pretranslated[key] = {
                "translated": translated,
                "voice_id": voice_id,
                "translate_time": translate_time,
                "translation_meta": translation_meta,
            }
            # Evict oldest if over limit
            while len(self._pretranslated) > 10:
                self._pretranslated.popitem(last=False)

    def set_atmosphere(self, pcm_bytes):
        """Set atmosphere PCM data."""
        self._atmosphere_pcm = pcm_bytes

    def set_atmosphere_enabled(self, enabled):
        """Toggle atmosphere mixing."""
        self._atmosphere_on = enabled
        if enabled:
            with self._atmosphere_lock:
                # Sync position to current video time so crowd noise matches the match moment
                if self.video_start:
                    elapsed = time.time() - self.video_start
                    self._atmosphere_pos = max(0, int(elapsed * 32000) // BYTES_PER_10MS * BYTES_PER_10MS)
                else:
                    self._atmosphere_pos = 0
        print(f"  [{self._vts()}] [ATMOS] {'ON' if enabled else 'OFF'} "
              f"(pcm={'yes' if self._atmosphere_pcm else 'NO'})")

    def set_original_audio(self, pcm_bytes):
        """Set original commentary PCM data."""
        self._original_pcm = pcm_bytes

    def set_original_enabled(self, enabled):
        """Toggle original audio mode. When on, TTS output is suppressed."""
        self._original_on = enabled
        if enabled:
            with self._original_lock:
                # Sync position to current video time
                if self.video_start:
                    elapsed = time.time() - self.video_start
                    self._original_pos = max(0, int(elapsed * 32000) // BYTES_PER_10MS * BYTES_PER_10MS)
                else:
                    self._original_pos = 0
            # When original on, disable atmosphere
            self._atmosphere_on = False
            # Clear translated SR and TTS buffers so stale content doesn't play
            # when original mode is toggled off later
            with self._sr_buf_lock:
                self._sr_audio_buf.clear()
                self._sr_playback_meta_slot = None
            with self._buf_lock:
                self._audio_buf.clear()
                self._playback_meta_slot = None
        print(f"  [{self._vts()}] [ORIG] {'ON' if enabled else 'OFF'}")

    def _get_original_chunk(self):
        """Get next 320-byte chunk of original audio, advancing position."""
        if not self._original_pcm:
            return None
        pcm_len = len(self._original_pcm)
        with self._original_lock:
            pos = self._original_pos
            if pos >= pcm_len:
                return None  # past end
            end = min(pos + BYTES_PER_10MS, pcm_len)
            chunk = self._original_pcm[pos:end]
            if len(chunk) < BYTES_PER_10MS:
                chunk = chunk + b'\x00' * (BYTES_PER_10MS - len(chunk))
            self._original_pos = pos + BYTES_PER_10MS
        return chunk

    def _mix_atmosphere_chunk(self, chunk):
        """Mix atmosphere audio into a 320-byte PCM chunk."""
        if not self._atmosphere_pcm:
            return chunk

        atmos_len = len(self._atmosphere_pcm)
        with self._atmosphere_lock:
            pos = self._atmosphere_pos
            if pos + BYTES_PER_10MS <= atmos_len:
                atmos_chunk = self._atmosphere_pcm[pos:pos + BYTES_PER_10MS]
                self._atmosphere_pos = pos + BYTES_PER_10MS
            else:
                remaining = atmos_len - pos
                atmos_chunk = (self._atmosphere_pcm[pos:]
                              + self._atmosphere_pcm[:BYTES_PER_10MS - remaining])
                self._atmosphere_pos = BYTES_PER_10MS - remaining

        n_samples = BYTES_PER_10MS // 2  # 160 samples per 10ms
        mixed = bytearray(BYTES_PER_10MS)
        vol = self._atmosphere_vol
        for i in range(n_samples):
            off = i * 2
            s1 = struct.unpack_from('<h', chunk, off)[0]
            s2 = struct.unpack_from('<h', atmos_chunk, off)[0]
            val = s1 + int(s2 * vol)
            val = max(-32768, min(32767, val))
            struct.pack_into('<h', mixed, off, val)
        return bytes(mixed)

    def _process_item(self, item, lang_version=None):
        """Translate + fetch TTS for a single queue item. Returns result dict or None on failure.
        Pushes audio to whatever buffer _tts_target_buf points to."""
        text, play_at, translate_fn, enqueued_at, target_duration_s = self._item_fields(item)

        with self._uid_lock:
            self._utterance_id += 1
            uid = self._utterance_id
        process_start = time.time()
        queue_wait = (process_start - enqueued_at) if enqueued_at else 0.0
        prepare_started_at = process_start

        # Check pre-translation cache before calling translate_fn
        pre_xlat_hit = False
        voice_id = self.voice_id
        translation_meta = None
        t_translate = time.monotonic()
        translate_started_at = time.time()
        cache_key = (text, play_at)
        with self._pretranslate_lock:
            cached = self._pretranslated.pop(cache_key, None)
        if cached:
            translated = cached["translated"]
            voice_id = cached["voice_id"]
            translate_time = cached["translate_time"]
            translation_meta = cached.get("translation_meta")
            pre_xlat_hit = True
        elif translate_fn:
            try:
                result = translate_fn(text)
                translated, voice_id, translation_meta = self._unpack_translate_result(result)
            except Exception:
                translated = text
            translate_time = time.monotonic() - t_translate
        else:
            translated = text
            translate_time = time.monotonic() - t_translate
        translate_ended_at = time.time()

        queued = self._text_queue.qsize()
        wc = len(translated.split())
        is_lookahead = self._tts_target_buf is self._lookahead_buf
        tag = "LOOKAHEAD " if is_lookahead else ""
        xlat_tag = "PRE-XLAT hit" if pre_xlat_hit else f"xlat: {translate_time:.2f}s"
        print(f"  [{self._vts()}] [TTS #{uid}] {tag}Starting — \"{translated[:50]}\" "
              f"({wc}w, queue: {queued}, {xlat_tag}, voice: {voice_id[:8]}, "
              f"q_wait: {queue_wait:.2f}s)")

        t0 = time.monotonic()
        tts_started_at = time.time()
        pcm_bytes = asyncio.run(self._tts_collect(translated, uid, voice_id=voice_id))
        tts_time = time.monotonic() - t0
        tts_ended_at = time.time()

        return {
            "uid": uid, "text": text, "translated": translated, "play_at": play_at,
            "voice_id": voice_id, "tts_time": tts_time, "translate_time": translate_time,
            "pre_translated": pre_xlat_hit, "queue_wait_ms": int(queue_wait * 1000),
            "target_duration_s": target_duration_s,
            "pcm_bytes": pcm_bytes or b"",
            "translation_model_used": (translation_meta or {}).get("model_used") if isinstance(translation_meta, dict) else None,
            "translation_fallback_reason": (translation_meta or {}).get("fallback_reason") if isinstance(translation_meta, dict) else None,
            "prepare_started_at": prepare_started_at,
            "translate_started_at": translate_started_at,
            "translate_ended_at": translate_ended_at,
            "tts_started_at": tts_started_at,
            "tts_ended_at": tts_ended_at,
            "ready_at": time.time(),
            "lang_version": lang_version,
        }

    def _submit_prepare(self, item):
        _, play_at, _, _, _ = self._item_fields(item)
        lang_version = self._lang_version
        with self._inflight_lock:
            self._inflight_play_ats.add(play_at)

        try:
            fut = self._prepare_executor.submit(self._process_item, item, lang_version)
        except RuntimeError:
            with self._inflight_lock:
                self._inflight_play_ats.discard(play_at)
            return None

        def _done(done_fut, item_play_at=play_at):
            with self._inflight_lock:
                self._inflight_play_ats.discard(item_play_at)
            self._prepare_done.put(done_fut)

        fut.add_done_callback(_done)
        return fut

    def _start_ready_result(self, result):
        pcm_bytes = result.get("pcm_bytes") or b""
        chunks = self._audio_bytes_to_chunks(pcm_bytes)
        buf_ms = len(chunks) * 10
        result.pop("pcm_bytes", None)
        result["total_buffered_ms"] = buf_ms
        with self._buf_lock:
            self._audio_buf.clear()
            self._audio_buf.extend(chunks)
            self._playback_meta_slot = result
        self._playback_ready.set()
        self._any_playback_ready.set()
        print(f"  [{self._vts()}] [TTS #{result.get('uid')}] Ready for pipe "
              f"({buf_ms}ms buffered)")

    def _is_stt_audio_active(self):
        if self._playback_ready.is_set():
            return True
        with self._buf_lock:
            return bool(self._audio_buf)

    def _emit_dropped_result(self, result, status="dropped", interrupted_by=""):
        pcm_bytes = result.pop("pcm_bytes", b"") or b""
        buf_ms = round(len(pcm_bytes) / (SAMPLE_RATE * 2) * 1000) if pcm_bytes else result.get("total_buffered_ms", 0)
        self._skipped_meta.append({
            "source": "stt", "status": status,
            "uid": result.get("uid"), "text": result.get("text"),
            "translated": result.get("translated"),
            "translate_time": result.get("translate_time"),
            "tts_time": result.get("tts_time"),
            "play_at": result.get("play_at"),
            "play_started_at": None, "play_ended_at": None,
            "actual_play_duration_ms": 0, "total_buffered_ms": buf_ms,
            "interrupted": False, "interrupted_by": interrupted_by,
            "pre_translated": result.get("pre_translated", False),
            "queue_wait_ms": result.get("queue_wait_ms", 0),
            "translation_model_used": result.get("translation_model_used"),
            "translation_fallback_reason": result.get("translation_fallback_reason"),
            "local_speed_factor": result.get("local_speed_factor"),
            "fit_from_ms": result.get("fit_from_ms"),
            "fit_to_ms": result.get("fit_to_ms"),
            "fit_deadline_ms": result.get("fit_deadline_ms"),
            "fit_cpu_ms": result.get("fit_cpu_ms"),
            "fit_reason": result.get("fit_reason"),
            "voice_id": result.get("voice_id"),
            "discarded_ms": buf_ms if status in ("replaced", "suppressed") else 0,
            "prepare_started_at": result.get("prepare_started_at"),
            "translate_started_at": result.get("translate_started_at"),
            "translate_ended_at": result.get("translate_ended_at"),
            "tts_started_at": result.get("tts_started_at"),
            "tts_ended_at": result.get("tts_ended_at"),
            "ready_at": result.get("ready_at"),
        })

    def _tts_worker(self):
        """Prepare STT utterances in parallel, then play ready audio by play_at order."""
        futures = set()
        idle_reported = False

        while not self._stop.is_set():
            # Keep at most two per-language translation+TTS jobs in flight.
            while len(futures) < 2 and not self._closing:
                try:
                    item = self._text_queue.get_nowait()
                except queue.Empty:
                    break
                self.is_speaking.set()
                idle_reported = False
                self._interrupt.clear()
                fut = self._submit_prepare(item)
                if fut:
                    futures.add(fut)

            # Move completed prepare jobs into a play_at heap.
            while True:
                try:
                    fut = self._prepare_done.get_nowait()
                except queue.Empty:
                    break
                futures.discard(fut)
                try:
                    result = fut.result()
                except Exception as e:
                    print(f"  [{self._vts()}] [TTS] prepare failed: {e}")
                    continue
                if not result:
                    continue
                if result.get("lang_version") != self._lang_version:
                    print(f"  [{self._vts()}] [TTS #{result.get('uid')}] "
                          "Discarded prepared audio (stale generation)")
                    self._emit_dropped_result(result, status="replaced", interrupted_by="stale_generation")
                    continue
                if not result.get("pcm_bytes"):
                    print(f"  [{self._vts()}] [TTS #{result.get('uid')}] DROPPED — no audio")
                    self._emit_dropped_result(result, status="dropped", interrupted_by="no_audio")
                    continue
                with self._ready_lock:
                    self._ready_seq += 1
                    heapq.heappush(self._ready_heap, (result.get("play_at") or time.time(), self._ready_seq, result))

            # Decide whether the earliest ready item can start.
            result = None
            with self._ready_lock:
                if self._ready_heap:
                    _, _, result = self._ready_heap[0]

            if result:
                play_at = result.get("play_at")
                now = time.time()
                if not result.get("_fit_checked"):
                    self._fit_result_audio_to_next_play_at(result)
                    result["_fit_checked"] = True

                if play_at and now < play_at:
                    self._next_stt_play_at = play_at
                else:
                    self._next_stt_play_at = None

                if self._is_stt_audio_active():
                    if play_at and now >= play_at:
                        self._interrupt.set()
                        deadline = time.monotonic() + 0.5
                        while time.monotonic() < deadline and self._is_stt_audio_active():
                            time.sleep(0.005)
                    else:
                        time.sleep(0.005)
                        continue

                now = time.time()
                if play_at and now < play_at:
                    wait_s = play_at - now
                    if wait_s > 0.05:
                        time.sleep(min(0.025, wait_s - 0.05))
                        continue
                    while time.time() < play_at and not self._stop.is_set():
                        pass

                with self._ready_lock:
                    if self._ready_heap and self._ready_heap[0][2] is result:
                        heapq.heappop(self._ready_heap)
                    else:
                        result = None
                if not result:
                    continue

                uid = result.get("uid")
                buf_ms = round(len(result.get("pcm_bytes") or b"") / (SAMPLE_RATE * 2) * 1000)
                late = (time.time() - play_at) if play_at else 0
                if play_at and late > self._late_start_grace_s:
                    pre_tag = "hit" if result.get("pre_translated") else "miss"
                    q_wait = result.get("queue_wait_ms", 0) / 1000
                    print(f"  [{self._vts()}] [TTS #{uid}] DROPPED {buf_ms}ms — {late:.2f}s past play_at "
                          f"(xlat={result['translate_time']:.2f}s, tts={result['tts_time']:.2f}s, "
                          f"queued_behind={q_wait:.2f}s, pre_xlat={pre_tag})")
                    self._emit_dropped_result(result, status="dropped")
                    continue

                if self._stt_suppressed.is_set():
                    print(f"  [{self._vts()}] [TTS #{uid}] Suppressed (SR GOAL playing)")
                    self._emit_dropped_result(result, status="suppressed")
                    self._stt_suppressed.clear()
                    continue

                if self._original_on:
                    self._emit_dropped_result(result, status="suppressed", interrupted_by="original")
                    continue

                print(f"  [{self._vts()}] [TTS #{uid}] Starting playback "
                      f"({buf_ms}ms, xlat={result['translate_time']:.2f}s, "
                      f"tts={result['tts_time']:.2f}s, q_wait={result.get('queue_wait_ms', 0) / 1000:.2f}s)")
                self._interrupt.clear()
                self._start_ready_result(result)
                continue

            self._next_stt_play_at = None
            if self._closing and not futures and self._text_queue.empty():
                with self._ready_lock:
                    ready_empty = not self._ready_heap
                if ready_empty:
                    break

            if not futures and self._text_queue.empty():
                if self.is_speaking.is_set() and not self._is_stt_audio_active():
                    self.is_speaking.clear()
                    if not idle_reported:
                        idle_reported = True
                        print(f"  [{self._vts()}] [TTS] Queue empty — idle")
                        if self.on_idle:
                            self.on_idle()
                time.sleep(0.05)
            else:
                time.sleep(0.005)

    async def _tts_collect(self, text, uid, voice_id=None):
        """Connect to ElevenLabs and return one utterance as local PCM bytes."""
        vid = voice_id or self.voice_id

        for attempt in range(2):
            send_text = text
            if attempt == 1:
                send_text = text + "..."
                print(f"  [{self._vts()}] [TTS #{uid}] Retrying with padded text")

            pcm_bytes = await self._tts_once_collect(send_text, uid, vid)
            if pcm_bytes:
                return pcm_bytes
            print(f"  [{self._vts()}] [TTS #{uid}] WARNING: No audio received from ElevenLabs"
                  f"{' (will retry)' if attempt == 0 else ''}")
        return b""

    async def _tts_once_collect(self, text, uid, voice_id):
        uri = (f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
               f"/stream-input?model_id={self.model}&output_format=pcm_16000")

        chunks = []
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "text": " ",
                    "voice_settings": {
                        "speed": self._elevenlabs_speed,
                        "stability": self._elevenlabs_stability,
                        "similarity_boost": self._elevenlabs_similarity_boost,
                    },
                    "xi_api_key": self.api_key,
                }))

                await ws.send(json.dumps({
                    "text": text,
                    "try_trigger_generation": True,
                }))

                await ws.send(json.dumps({"text": ""}))

                async for message in ws:
                    data = json.loads(message)

                    if data.get("audio"):
                        chunks.append(base64.b64decode(data["audio"]))
                        if len(chunks) == 1:
                            print(f"  [{self._vts()}] [TTS #{uid}] First audio chunk received")

                    if data.get("isFinal"):
                        break
        except Exception as e:
            print(f"  [{self._vts()}] [TTS #{uid}] ERROR: {e}")
            return b""

        return b"".join(chunks)

    async def _tts(self, text, uid, voice_id=None):
        """Connect to ElevenLabs WebSocket, send text, buffer all PCM.
        Retries once if no audio received (common with very short phrases)."""
        vid = voice_id or self.voice_id

        for attempt in range(2):
            send_text = text
            if attempt == 1:
                # Pad short text on retry — ElevenLabs sometimes fails on very short inputs
                send_text = text + "..."
                print(f"  [{self._vts()}] [TTS #{uid}] Retrying with padded text")

            chunk_count = await self._tts_once(send_text, uid, vid)
            if chunk_count > 0 or self._interrupt.is_set():
                break
            print(f"  [{self._vts()}] [TTS #{uid}] WARNING: No audio received from ElevenLabs"
                  f"{' (will retry)' if attempt == 0 else ''}")

    async def _tts_once(self, text, uid, voice_id):
        """Single ElevenLabs WebSocket TTS attempt. Returns chunk count."""
        uri = (f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
               f"/stream-input?model_id={self.model}&output_format=pcm_16000")

        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "text": " ",
                    "voice_settings": {
                        "speed": self._elevenlabs_speed,
                        "stability": self._elevenlabs_stability,
                        "similarity_boost": self._elevenlabs_similarity_boost,
                    },
                    "xi_api_key": self.api_key,
                }))

                await ws.send(json.dumps({
                    "text": text,
                    "try_trigger_generation": True,
                }))

                await ws.send(json.dumps({"text": ""}))

                chunk_count = 0
                async for message in ws:
                    if self._interrupt.is_set():
                        break

                    data = json.loads(message)

                    if data.get("audio"):
                        pcm_bytes = base64.b64decode(data["audio"])
                        self._push_audio(pcm_bytes)
                        chunk_count += 1
                        if chunk_count == 1:
                            print(f"  [{self._vts()}] [TTS #{uid}] First audio chunk received")

                    if data.get("isFinal"):
                        break

                return chunk_count

        except Exception as e:
            print(f"  [{self._vts()}] [TTS #{uid}] ERROR: {e}")
            return 0

    def stop(self):
        # Shut down pre-translation executor early (don't wait for in-flight)
        self._pretranslate_executor.shutdown(wait=False)
        self._prepare_executor.shutdown(wait=False)
        # Phase 1: Close — reject new work, wake tts_worker to exit
        self._closing = True
        self._interrupt.set()
        if self._tts_worker_thread:
            self._tts_worker_thread.join(timeout=2.0)
        # Phase 2: Stop — pipe_writer can now exit safely (no more slot writes)
        self._stop.set()
        self._any_playback_ready.set()  # wake pipe_writer to drain and exit
        if self._pipe_writer_thread:
            self._pipe_writer_thread.join(timeout=1.0)
