import asyncio
import base64
import collections
import json
import os
import queue
import struct
import threading
import time

import websockets

from lib.constants import BYTES_PER_10MS, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL


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
      - speak() queues text. Only real INTERRUPT events clear the queue.
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
        # Stats
        self._utterance_id = 0
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
                        })
                    except Exception:
                        pass
                continue

            # Skip SR if STT is arriving before it would finish
            if source == "SR":
                stt_due = self._next_stt_play_at
                if stt_due:
                    sr_end = time.time() + n_chunks * 0.01
                    if stt_due < sr_end:
                        meta = current_meta or {}
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

            while not self._stop.is_set() and not self._interrupt.is_set():
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

            # Detect interrupt-caused breakout (STT only)
            if source == "STT" and self._interrupt.is_set() and not was_interrupted:
                was_interrupted = True
                interrupted_by = "speak_interrupt"

            # Use metadata captured at playback start (not post-playback pop)
            meta = current_meta or {}

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

    def speak(self, text, interrupt=False, play_at=None, translate_fn=None):
        """
        Non-blocking. Queues text for sequential TTS playback.
        text: English text to speak (will be translated just-in-time if translate_fn set)
        play_at: absolute time.time() when playback should start (pre-fetch + hold)
        translate_fn: callable(text) -> translated_text (uses current lang at TTS time)
        """
        if self._closing:
            return
        if interrupt:
            self._interrupt.set()
            self._stt_suppressed.clear()
            with self._buf_lock:
                self._audio_buf.clear()
                self._playback_meta_slot = None
            with self._sr_buf_lock:
                self._sr_audio_buf.clear()
                self._sr_playback_meta_slot = None
            with self._lookahead_lock:
                self._lookahead_buf.clear()
            self._lookahead_item = None
            self._sr_playback_ready.clear()
            int_discarded = 0
            while not self._text_queue.empty():
                try:
                    stale_item = self._text_queue.get_nowait()
                    int_discarded += 1
                    stale_text, stale_play_at, _ = stale_item if isinstance(stale_item, tuple) else (stale_item, None, None)
                    self._skipped_meta.append({
                        "source": "stt", "status": "dropped",
                        "uid": None, "text": stale_text,
                        "translated": None,
                        "translate_time": None, "tts_time": None,
                        "play_at": stale_play_at,
                        "play_started_at": None, "play_ended_at": None,
                        "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                        "interrupted": False, "interrupted_by": "speak_interrupt",
                    })
                except queue.Empty:
                    break
            # Also emit for lookahead item being discarded
            if self._lookahead_item:
                la = self._lookahead_item
                self._skipped_meta.append({
                    "source": "stt", "status": "dropped",
                    "uid": la.get("uid"), "text": la.get("text"),
                    "translated": la.get("translated"),
                    "translate_time": la.get("translate_time"),
                    "tts_time": la.get("tts_time"),
                    "play_at": la.get("play_at"),
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                    "interrupted": False, "interrupted_by": "speak_interrupt",
                })
                int_discarded += 1
                self._lookahead_item = None
            print(f"  [{self._vts()}] [TTS] Interrupted — STT+SR queues cleared"
                  f"{f' ({int_discarded} queued items discarded)' if int_discarded else ''}")
        if text:
            if play_at and not interrupt:
                discarded = 0
                while not self._text_queue.empty():
                    try:
                        stale_item = self._text_queue.get_nowait()
                        discarded += 1
                        # Emit telemetry for replaced queue items
                        stale_text, stale_play_at, _ = stale_item if isinstance(stale_item, tuple) else (stale_item, None, None)
                        self._skipped_meta.append({
                            "source": "stt", "status": "replaced",
                            "uid": None, "text": stale_text,
                            "translated": None,
                            "translate_time": None, "tts_time": None,
                            "play_at": stale_play_at,
                            "play_started_at": None, "play_ended_at": None,
                            "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                            "interrupted": False, "interrupted_by": "",
                        })
                    except queue.Empty:
                        break
                # Also discard lookahead if it was based on a now-stale item
                if self._lookahead_item:
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
                    })
                if discarded:
                    print(f"  [{self._vts()}] [TTS] Replaced {discarded} stale queued item(s)")
            self._text_queue.put((text, play_at, translate_fn))

    def clear_stt(self):
        """
        Clear STT queue and audio without interrupting SR playback.
        Used when an SR INTERRUPT event (e.g. GOAL) is already playing
        in _sr_audio_buf and we want to prevent STT from preempting it.
        Sets _stt_suppressed so the TTS worker discards its in-flight
        utterance instead of signaling _playback_ready.
        """
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
            })
        self._lookahead_item = None
        self._playback_ready.clear()
        stt_cleared = 0
        while not self._text_queue.empty():
            try:
                stale_item = self._text_queue.get_nowait()
                stt_cleared += 1
                stale_text, stale_play_at, _ = stale_item if isinstance(stale_item, tuple) else (stale_item, None, None)
                self._skipped_meta.append({
                    "source": "stt", "status": "suppressed",
                    "uid": None, "text": stale_text,
                    "translated": None,
                    "translate_time": None, "tts_time": None,
                    "play_at": stale_play_at,
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": 0,
                    "interrupted": False, "interrupted_by": "clear_stt",
                })
            except queue.Empty:
                break
        print(f"  [{self._vts()}] [TTS] STT cleared (SR playback preserved)"
              f"{f' ({stt_cleared} queued items suppressed)' if stt_cleared else ''}")

    def queue_size(self):
        return self._text_queue.qsize()

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

    def _process_item(self, item):
        """Translate + fetch TTS for a single queue item. Returns result dict or None on failure.
        Pushes audio to whatever buffer _tts_target_buf points to."""
        if isinstance(item, tuple):
            text, play_at, translate_fn = item
        else:
            text, play_at, translate_fn = item, None, None

        self._utterance_id += 1
        uid = self._utterance_id

        # Just-in-time translation with current language + voice
        voice_id = self.voice_id
        t_translate = time.monotonic()
        if translate_fn:
            try:
                result = translate_fn(text)
                if isinstance(result, tuple):
                    translated, voice_id = result
                else:
                    translated = result
            except Exception:
                translated = text
        else:
            translated = text
        translate_time = time.monotonic() - t_translate

        queued = self._text_queue.qsize()
        wc = len(translated.split())
        is_lookahead = self._tts_target_buf is self._lookahead_buf
        tag = "LOOKAHEAD " if is_lookahead else ""
        print(f"  [{self._vts()}] [TTS #{uid}] {tag}Starting — \"{translated[:50]}\" "
              f"({wc}w, queue: {queued}, xlat: {translate_time:.2f}s, voice: {voice_id[:8]})")

        t0 = time.monotonic()
        self._loop.run_until_complete(self._tts(translated, uid, voice_id=voice_id))
        tts_time = time.monotonic() - t0

        if self._interrupt.is_set():
            print(f"  [{self._vts()}] [TTS #{uid}] {tag}Interrupted after {tts_time:.2f}s")
            return None

        return {
            "uid": uid, "text": text, "translated": translated, "play_at": play_at,
            "voice_id": voice_id, "tts_time": tts_time, "translate_time": translate_time,
        }

    def _tts_worker(self):
        """Processes TTS requests with lookahead — translates+fetches the next utterance
        while the current one is playing, so playback time doesn't eat into the next
        utterance's timing budget."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while not self._stop.is_set():
            # Check for a lookahead result first (already translated + TTS'd)
            result = None
            if self._lookahead_item:
                result = self._lookahead_item
                self._lookahead_item = None
                # Move lookahead audio into main buffer (copy under lock, then swap)
                with self._lookahead_lock:
                    la_chunks = list(self._lookahead_buf)
                    self._lookahead_buf.clear()
                with self._buf_lock:
                    self._audio_buf.clear()
                    self._audio_buf.extend(la_chunks)
                uid = result["uid"]
                print(f"  [{self._vts()}] [TTS #{uid}] Using lookahead result "
                      f"({len(self._audio_buf) * 10}ms buffered)")
            else:
                # Nothing pre-processed — pull from queue
                try:
                    item = self._text_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._closing:
                        break  # shutdown: no more items, exit
                    if self.is_speaking.is_set():
                        self.is_speaking.clear()
                        print(f"  [{self._vts()}] [TTS] Queue empty — idle")
                        if self.on_idle:
                            self.on_idle()
                    continue

                self.is_speaking.set()
                self._interrupt.clear()
                self._playback_ready.clear()

                # Process into main audio buffer
                self._tts_target_buf = None  # default = _audio_buf
                self._tts_target_lock = None
                result = self._process_item(item)
                if not result:
                    with self._buf_lock:
                        buf_ms = len(self._audio_buf) * 10
                        self._audio_buf.clear()
                    # Emit telemetry for dropped-during-TTS items
                    item_text, item_play_at, _ = item if isinstance(item, tuple) else (item, None, None)
                    self._skipped_meta.append({
                        "source": "stt", "status": "dropped",
                        "uid": None, "text": item_text,
                        "translated": None,
                        "translate_time": None, "tts_time": None,
                        "play_at": item_play_at,
                        "play_started_at": None, "play_ended_at": None,
                        "actual_play_duration_ms": 0, "total_buffered_ms": buf_ms,
                        "interrupted": False, "interrupted_by": "tts_interrupt",
                    })
                    continue

                uid = result["uid"]

            self.is_speaking.set()
            self._interrupt.clear()
            self._playback_ready.clear()

            # SR GOAL is playing — discard this STT utterance
            if self._stt_suppressed.is_set():
                print(f"  [{self._vts()}] [TTS #{uid}] Suppressed (SR GOAL playing)")
                with self._buf_lock:
                    buf_ms = len(self._audio_buf) * 10
                    self._audio_buf.clear()
                self._skipped_meta.append({
                    "source": "stt", "status": "suppressed",
                    "uid": result["uid"], "text": result["text"],
                    "translated": result["translated"],
                    "translate_time": result["translate_time"],
                    "tts_time": result["tts_time"],
                    "play_at": result["play_at"],
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": buf_ms,
                    "interrupted": False, "interrupted_by": "",
                })
                self._stt_suppressed.clear()
                continue

            buf_chunks = len(self._audio_buf)
            buf_ms = buf_chunks * 10
            tts_time = result["tts_time"]
            play_at = result["play_at"]

            # Wait until scheduled play time if set
            if play_at:
                wait_s = play_at - time.time()
                if wait_s > 0:
                    print(f"  [{self._vts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — "
                          f"holding {wait_s:.2f}s for sync")
                    # Signal pipe_writer that STT is coming at this time
                    self._next_stt_play_at = play_at
                    # Interruptible coarse sleep for the bulk of the wait
                    coarse = wait_s - 0.05
                    if coarse > 0:
                        self._interrupt.wait(timeout=coarse)
                    # Tight spin for the final ~50ms to hit ±1ms
                    while time.time() < play_at and not self._interrupt.is_set():
                        pass
                    self._next_stt_play_at = None
                else:
                    late = -wait_s
                    print(f"  [{self._vts()}] [TTS #{uid}] DROPPED {buf_ms}ms — {late:.2f}s past play_at")
                    with self._buf_lock:
                        self._audio_buf.clear()
                    self._skipped_meta.append({
                        "source": "stt", "status": "dropped",
                        "uid": result["uid"], "text": result["text"],
                        "translated": result["translated"],
                        "translate_time": result["translate_time"],
                        "tts_time": result["tts_time"],
                        "play_at": play_at,
                        "play_started_at": None, "play_ended_at": None,
                        "actual_play_duration_ms": 0, "total_buffered_ms": buf_ms,
                        "interrupted": False, "interrupted_by": "",
                    })
                    continue
            else:
                print(f"  [{self._vts()}] [TTS #{uid}] Buffered {buf_ms}ms in {tts_time:.2f}s — starting playback")

            # If original audio is playing, discard translated TTS
            if self._original_on:
                with self._buf_lock:
                    buf_ms_orig = len(self._audio_buf) * 10
                    self._audio_buf.clear()
                self._skipped_meta.append({
                    "source": "stt", "status": "suppressed",
                    "uid": result["uid"], "text": result["text"],
                    "translated": result["translated"],
                    "translate_time": result["translate_time"],
                    "tts_time": result["tts_time"],
                    "play_at": result["play_at"],
                    "play_started_at": None, "play_ended_at": None,
                    "actual_play_duration_ms": 0, "total_buffered_ms": buf_ms_orig,
                    "interrupted": False, "interrupted_by": "original",
                })
                continue

            # Set metadata slot for pipe_writer telemetry enrichment
            with self._buf_lock:
                self._playback_meta_slot = result
            # Signal pipe writer that full utterance is ready
            self._playback_ready.set()
            self._any_playback_ready.set()

            # While playback drains, try to pre-process the next queued item (lookahead)
            drain_start = time.monotonic()
            lookahead_done = False

            while self._audio_buf and not self._interrupt.is_set():
                # Try lookahead if we haven't already and there's a queued item
                if not lookahead_done and not self._text_queue.empty():
                    try:
                        next_item = self._text_queue.get_nowait()
                    except queue.Empty:
                        next_item = None

                    if next_item:
                        # Redirect TTS output to lookahead buffer
                        with self._lookahead_lock:
                            self._lookahead_buf.clear()
                        self._tts_target_buf = self._lookahead_buf
                        self._tts_target_lock = self._lookahead_lock
                        lang_v = self._lang_version
                        la_result = self._process_item(next_item)
                        self._tts_target_buf = None
                        self._tts_target_lock = None

                        if la_result and not self._interrupt.is_set():
                            # Discard if language changed during processing
                            if self._lang_version != lang_v:
                                print(f"  [{self._vts()}] [TTS #{la_result['uid']}] "
                                      f"Lookahead discarded (language changed)")
                                with self._lookahead_lock:
                                    la_ms = len(self._lookahead_buf) * 10
                                    self._lookahead_buf.clear()
                                self._skipped_meta.append({
                                    "source": "stt", "status": "dropped",
                                    "uid": la_result["uid"], "text": la_result["text"],
                                    "translated": la_result["translated"],
                                    "translate_time": la_result["translate_time"],
                                    "tts_time": la_result["tts_time"],
                                    "play_at": la_result["play_at"],
                                    "play_started_at": None, "play_ended_at": None,
                                    "actual_play_duration_ms": 0, "total_buffered_ms": la_ms,
                                    "interrupted": False, "interrupted_by": "lang_change",
                                })
                            else:
                                self._lookahead_item = la_result
                                print(f"  [{self._vts()}] [TTS #{la_result['uid']}] Lookahead ready "
                                      f"({len(self._lookahead_buf) * 10}ms)")
                        else:
                            with self._lookahead_lock:
                                la_ms = len(self._lookahead_buf) * 10
                                self._lookahead_buf.clear()
                            # Emit telemetry for lookahead dropped during TTS
                            if not la_result and next_item:
                                la_text, la_play_at, _ = next_item if isinstance(next_item, tuple) else (next_item, None, None)
                                self._skipped_meta.append({
                                    "source": "stt", "status": "dropped",
                                    "uid": None, "text": la_text,
                                    "translated": None,
                                    "translate_time": None, "tts_time": None,
                                    "play_at": la_play_at,
                                    "play_started_at": None, "play_ended_at": None,
                                    "actual_play_duration_ms": 0, "total_buffered_ms": la_ms,
                                    "interrupted": False, "interrupted_by": "lookahead_interrupt",
                                })
                    lookahead_done = True

                time.sleep(0.01)

            drain_time = time.monotonic() - drain_start
            la_tag = " +lookahead" if self._lookahead_item else ""
            print(f"  [{self._vts()}] [TTS #{uid}] Done — "
                  f"total: {tts_time + drain_time:.2f}s (tts: {tts_time:.2f}s + play: {drain_time:.2f}s){la_tag}")

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
                        "stability": 0.5,
                        "similarity_boost": 0.8,
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
