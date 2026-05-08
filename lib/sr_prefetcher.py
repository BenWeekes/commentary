import asyncio
import base64
import json
import os
import queue
import threading
import time

import websockets

from lib.constants import BYTES_PER_10MS, SAMPLE_RATE, ELEVENLABS_MODEL
from lib.tts_engine import _ts


class SRPrefetcher:
    """
    Fetches ElevenLabs TTS for Sportradar events in parallel with the STT
    pipeline, then injects audio into the TTSEngine's SR buffer at the
    scheduled play_at time.

    Architecture:
      _feeder_worker:    drip-feeds events from _all_events into _prefetch_queue
                         using a rolling window (PREFETCH_HORIZON_S ahead)
      _prefetch_worker:  dequeues events, translates, fetches TTS → _ready_events
      _scheduler_worker: polls _ready_events, waits for play_at, injects into
                         tts._sr_audio_buf at the right moment

    On language change, flush() clears prefetched audio and resets the feeder
    so upcoming events are re-translated in the new language.
    """

    PREFETCH_HORIZON_S = 30  # only prefetch events within this window

    def __init__(self, tts_engine, api_key=None, model=ELEVENLABS_MODEL):
        self.tts = tts_engine
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.model = model
        self._stop = threading.Event()
        self._prefetch_queue = queue.Queue()
        self._ready_events = {}  # event_id → (pcm_bytes, play_at)
        self._ready_lock = threading.Lock()
        self._next_id = 0
        # All events list — feeder drip-feeds from here
        self._all_events = []    # [(text, play_at, translate_fn_factory)]
        self._all_events_lock = threading.Lock()
        self._feed_idx = 0       # next event to feed
        self._fetched_eids = set()  # event indices already in prefetch/ready

    def set_events(self, events):
        """Set the full event list. events = [(text, play_at, translate_fn_factory)]"""
        with self._all_events_lock:
            self._all_events = list(events)
            self._feed_idx = 0
            self._fetched_eids.clear()

    def schedule(self, text, play_at, translate_fn):
        """Schedule an SR event for prefetching and timed playback."""
        self._next_id += 1
        eid = self._next_id
        self._prefetch_queue.put((eid, text, play_at, translate_fn))
        return eid

    def flush(self):
        """Flush prefetched audio and reset feeder (called on language change).

        Drains the prefetch queue, clears ready events, and resets the feeder
        index so all future events are re-translated in the new language.
        """
        # Drain prefetch queue
        while not self._prefetch_queue.empty():
            try:
                self._prefetch_queue.get_nowait()
            except queue.Empty:
                break
        # Clear ready events (already-fetched TTS in old language)
        with self._ready_lock:
            cleared = len(self._ready_events)
            self._ready_events.clear()
        # Reset feeder — skip events whose play_at already passed
        with self._all_events_lock:
            now = time.time()
            self._fetched_eids.clear()
            self._feed_idx = 0
            # Advance past events that already played
            while self._feed_idx < len(self._all_events):
                _, play_at, _ = self._all_events[self._feed_idx]
                if play_at > now:
                    break
                self._feed_idx += 1
        if cleared:
            print(f"  [{self._vts()}] [SR] Flushed {cleared} prefetched events (language change)")

    def cancel_all(self):
        """Clear all pending and ready events (called on INTERRUPT)."""
        while not self._prefetch_queue.empty():
            try:
                self._prefetch_queue.get_nowait()
            except queue.Empty:
                break
        with self._ready_lock:
            self._ready_events.clear()

    def cancel_before(self, cutoff_play_at):
        """Cancel ready events that play before cutoff_play_at, keep the rest.

        Used by INTERRUPT events: the INTERRUPT and anything after it are
        preserved so a GOAL doesn't permanently silence later commentary.
        """
        with self._ready_lock:
            to_remove = [eid for eid, (_, play_at, *_rest) in self._ready_events.items()
                         if play_at < cutoff_play_at]
            for eid in to_remove:
                del self._ready_events[eid]

    def start(self):
        """Spawn feeder, prefetch, and scheduler worker threads."""
        threading.Thread(target=self._feeder_worker, daemon=True).start()
        threading.Thread(target=self._prefetch_worker, daemon=True).start()
        threading.Thread(target=self._scheduler_worker, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _vts(self):
        return _ts(self.tts.video_start)

    def _feeder_worker(self):
        """Drip-feed events into _prefetch_queue using a rolling window.

        Only events whose play_at is within PREFETCH_HORIZON_S of now are
        submitted for prefetch. Checks every 1s for newly eligible events.
        """
        while not self._stop.is_set():
            now = time.time()
            horizon = now + self.PREFETCH_HORIZON_S

            with self._all_events_lock:
                while self._feed_idx < len(self._all_events):
                    text, play_at, translate_fn_factory = self._all_events[self._feed_idx]
                    if play_at > horizon:
                        break  # not yet in window
                    idx = self._feed_idx
                    self._feed_idx += 1
                    if idx in self._fetched_eids:
                        continue  # already fed (shouldn't happen, but safe)
                    self._fetched_eids.add(idx)
                    self._next_id += 1
                    eid = self._next_id
                    self._prefetch_queue.put((eid, text, play_at, translate_fn_factory()))

            time.sleep(1.0)

    def _prefetch_worker(self):
        """Dequeue events, translate, fetch TTS audio, store in _ready_events."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self._stop.is_set():
            try:
                item = self._prefetch_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            eid, text, play_at, translate_fn = item

            # Translate
            voice_id = self.tts.voice_id
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

            lead_time = play_at - time.time()
            print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Fetching — "
                  f"\"{translated[:50]}\" (play in {lead_time:.1f}s)")

            # Fetch TTS
            t0 = time.monotonic()
            pcm_bytes = loop.run_until_complete(
                self._fetch_tts(translated, voice_id, eid)
            )
            fetch_time = time.monotonic() - t0

            if pcm_bytes and len(pcm_bytes) > 0:
                meta = {
                    "text": text, "translated": translated,
                    "translate_time": None,  # not separately timed in SR
                    "tts_time": fetch_time, "play_at": play_at,
                }
                with self._ready_lock:
                    self._ready_events[eid] = (pcm_bytes, play_at, meta)
                lead = play_at - time.time()
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Ready — "
                      f"{len(pcm_bytes)}B in {fetch_time:.2f}s, "
                      f"{lead:.2f}s before play_at")
            else:
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] WARNING: No audio received")

        loop.close()

    async def _fetch_tts(self, text, voice_id, eid):
        """
        Fetch TTS from ElevenLabs WebSocket. Same protocol as TTSEngine._tts_once
        but returns concatenated PCM bytes instead of pushing to shared buffer.
        Retries once with padded text on zero audio.
        """
        for attempt in range(2):
            send_text = text
            if attempt == 1:
                send_text = text + "..."
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] Retrying with padded text")

            pcm_parts = []
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
                        "text": send_text,
                        "try_trigger_generation": True,
                    }))
                    await ws.send(json.dumps({"text": ""}))

                    async for message in ws:
                        if self._stop.is_set():
                            return b""
                        data = json.loads(message)
                        if data.get("audio"):
                            pcm_parts.append(base64.b64decode(data["audio"]))
                        if data.get("isFinal"):
                            break

                result = b"".join(pcm_parts)
                if len(result) > 0:
                    return result
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] WARNING: No audio"
                      f"{' (will retry)' if attempt == 0 else ''}")

            except Exception as e:
                print(f"  [{self._vts()}] [SR PREFETCH #{eid}] ERROR: {e}")
                if attempt == 0:
                    continue
                return b""

        return b""

    def _scheduler_worker(self):
        """
        Polls _ready_events for events whose play_at is approaching.
        Uses two-phase wait: coarse sleep + tight spin for ±1ms precision.
        Injects PCM chunks into tts._sr_audio_buf at the right moment.
        """
        while not self._stop.is_set():
            # Find the next event to play
            now = time.time()
            next_eid = None
            next_play_at = None

            with self._ready_lock:
                for eid, (pcm_bytes, play_at, _meta) in self._ready_events.items():
                    if next_play_at is None or play_at < next_play_at:
                        next_eid = eid
                        next_play_at = play_at

            if next_eid is None:
                time.sleep(0.01)
                continue

            now = time.time()
            wait = next_play_at - now

            # Not yet time — sleep and re-check
            if wait > 0.1:
                time.sleep(min(wait - 0.05, 0.05))
                continue

            # Close to play_at — extract the event
            with self._ready_lock:
                entry = self._ready_events.pop(next_eid, None)
            if entry is None:
                continue

            pcm_bytes, play_at, meta = entry

            # Coarse sleep for bulk of remaining wait
            remaining = play_at - time.time()
            if remaining > 0.05:
                time.sleep(remaining - 0.05)

            # Tight spin for final ~50ms
            while time.time() < play_at and not self._stop.is_set():
                pass

            delta_ms = (time.time() - play_at) * 1000
            dur_ms = len(pcm_bytes) / (SAMPLE_RATE * 2) * 1000

            # Skip SR injection while original audio mode is active
            if self.tts._original_on:
                continue

            # Split into 10ms chunks and inject into SR buffer
            with self.tts._sr_buf_lock:
                offset = 0
                while offset < len(pcm_bytes):
                    end = offset + BYTES_PER_10MS
                    chunk = pcm_bytes[offset:end]
                    if len(chunk) < BYTES_PER_10MS:
                        chunk = chunk + b'\x00' * (BYTES_PER_10MS - len(chunk))
                    self.tts._sr_audio_buf.append(chunk)
                    offset = end
                self.tts._sr_playback_meta_slot = meta

            # Signal pipe writer
            self.tts._sr_playback_ready.set()
            self.tts._any_playback_ready.set()

            print(f"  [{self._vts()}] [SR SCHED #{next_eid}] Injected — "
                  f"{dur_ms:.0f}ms, delta {delta_ms:+.0f}ms")
