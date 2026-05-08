# L2 — TTSEngine Internals

> **When to Read This:** You are modifying the TTSEngine class, debugging audio playback or buffering issues, changing the atmosphere mixing logic, or investigating pipe writer timing.

## Overview

`TTSEngine` (`lib/tts_engine.py`) manages ElevenLabs WebSocket TTS and PCM audio delivery to the Go publisher.

## Threading Architecture

```
speak() ──▶ _text_queue ──▶ _tts_worker thread ──▶ _audio_buf ──▶ _pipe_writer thread ──▶ stdin pipe
   │              │                │                     │                │
   │         Queue()           asyncio loop          deque()         10ms timer
   │                          (per thread)        (thread-safe)
   │                               │
   │                          (during drain)
   │                               ├──▶ _lookahead_buf ──▶ next item pre-processed
   │                               │     (translate + TTS fetched in parallel)
   └── interrupt=True clears queue + buffer + lookahead
```

### _tts_worker thread

- Runs its own `asyncio` event loop (`asyncio.new_event_loop()`)
- Processes `_text_queue` items with **lookahead**: while the current utterance plays, the next one is translated and TTS'd in parallel
- For each item:
  1. Checks `_lookahead_item` — if present, uses pre-processed result (skip to step 5)
  2. Calls `translate_fn(text)` if provided (JIT translation)
  3. Connects to ElevenLabs WebSocket
  4. Sends text, receives base64-encoded PCM audio chunks → `_audio_buf`
  5. Waits for `play_at` time if scheduled (drops if late)
  6. Sets `_playback_ready` event when all audio is received (full pre-buffer)
  7. While pipe writer drains the buffer, grabs next queue item and processes into `_lookahead_buf`

### _pipe_writer thread

- Blocks on `_any_playback_ready.wait(timeout=0.005)` — wakes on TTS or SR audio, or times out for atmosphere
- Drains `_audio_buf` (STT) or `_sr_audio_buf` (SR) at exactly 10ms intervals, STT has priority
- Writes 320-byte chunks to `self.audio_pipe` (Go publisher stdin)
- When atmosphere is enabled: mixes atmosphere into TTS/SR chunks, and writes atmosphere-only during idle
- When atmosphere is off and no audio is playing: writes nothing (Go publisher handles silence)
- Logs underruns if buffer empties mid-playback

## Buffer Strategy

The engine uses **full pre-buffering**: the entire utterance is downloaded from ElevenLabs before playback starts. This eliminates underruns from network jitter.

```
Without lookahead (serial):
  t0 ── xlat+TTS ── t1 ── wait ── t2 ── playback ── t3 ── xlat+TTS ── t4 ── wait ── t5
                                                      │                               │
                                                      └── next item blocked until here

With lookahead (overlapped):
  t0 ── xlat+TTS ── t1 ── wait ── t2 ──── playback ──── t3
                                    │    ↑ next xlat+TTS ↑ │ ── wait ── t4 ── playback ── t5
                                    │    (in _lookahead_buf)
                                    └── pipe writer starts draining
```

The lookahead saves the full playback duration (typically 3-5s) from the next item's timing budget.

## Scheduling

`speak(text, play_at=timestamp)` schedules playback to start at a specific wall-clock time. The TTS worker fetches audio immediately but holds playback until `play_at`. This is used by the events fallback to sync commentary with delayed video:

```python
play_at = match_time_start + event_offset
```

### Precision targeting

Utterances must play at exact play_at time or be dropped. The hold uses a two-phase approach for sub-10ms accuracy:
1. **Coarse sleep**: `time.sleep(wait_s - 0.05)` — sleeps until 50ms before target
2. **Tight spin**: busy-wait `while time.time() < play_at` — hits ±1ms

The pipe writer blocks on `threading.Event.wait()` instead of polling, so it wakes within microseconds of `_playback_ready.set()`. Combined, the total chain from `play_at` to first PCM byte on stdin is <5ms.

## Interrupt Flow

1. `speak(text, interrupt=True)` is called
2. `_interrupt` event is set
3. `_audio_buf`, `_sr_audio_buf`, and `_lookahead_buf` are cleared (under locks)
4. `_lookahead_item` is discarded
5. `_text_queue` is drained
6. New text is queued
7. `_tts_worker` checks `_interrupt` before and after TTS — skips if set
8. `_interrupt` is cleared when the next non-interrupt item starts

## State Tracking

- `is_speaking` event: set when TTS worker is processing, cleared when queue empties
- `on_idle` callback: called when queue empties (used for external coordination)
- `_utterance_id`: monotonically increasing counter for log correlation

## Telemetry Metadata Slots

`TTSEngine` carries metadata alongside audio so playback telemetry can be enriched with the original text, translated text, timings, and scheduled `play_at`.

### Why single-slot (not FIFO)

`_tts_worker` produces result dicts, but `_pipe_writer` is the component that knows whether an item was actually played, interrupted mid-playback, dropped, or suppressed.

A single-slot design is safe because `_pipe_writer` captures the slot into a local variable at playback START (under the same lock that checks `n_chunks`). Any mid-playback clear of the slot (e.g. from `speak(interrupt=True)`) is harmless — the local copy is already captured. A FIFO deque was previously used but caused cascading metadata offset when `speak(interrupt=True)` cleared the deque mid-playback.

### Slots

- `_playback_meta_slot` — current STT item metadata; protected by `_buf_lock`
- `_sr_playback_meta_slot` — current SR item metadata; protected by `_sr_buf_lock`
- `_skipped_meta` — STT items that were dropped or suppressed before playback; drained by `_pipe_writer`

### Flow

1. `_tts_worker` processes an STT item into audio + result metadata
2. Before signaling `_playback_ready`, it sets `_playback_meta_slot = result` under `_buf_lock`
3. `_pipe_writer` captures the slot into `current_meta` under lock at the top of the playback cycle (before checking `n_chunks`), then clears the slot
4. After playback ends, `_pipe_writer` uses `current_meta` to emit one telemetry record via `on_telemetry`
5. Dropped/suppressed items are queued into `_skipped_meta` and emitted from `_pipe_writer` so telemetry stays single-threaded from the consumer side

### Status semantics

- `played` — utterance started and completed normally
- `interrupted` — playback started but was cut short (only set by `_pipe_writer` when `_interrupt` is detected during active playback)
- `dropped` — item never started playback (cleared from queue by `speak(interrupt=True)`, TTS returned no audio, or shutdown)
- `suppressed` — STT utterance was discarded because SR was already occupying the slot

Items that never played (queue clears, TTS failures, shutdown) are always `dropped`, not `interrupted`. Only `_pipe_writer` can set `interrupted` — it detects `_interrupt.is_set()` during active chunk drain.

## Shutdown

`stop()` uses a two-phase approach to prevent final telemetry loss:

1. **Phase 1 (closing)**: Sets `_closing = True`, sets `_interrupt` to wake `_tts_worker`. Joins `_tts_worker_thread` (timeout 2s). The worker finishes any in-flight item, writes the final slot, then exits on empty queue + `_closing`.
2. **Phase 2 (stopped)**: Sets `_stop`, wakes `_pipe_writer` via `_any_playback_ready`. Joins `_pipe_writer_thread` (timeout 1s). The writer drains any final slot and `_skipped_meta`, then exits.

`speak()` returns immediately when `_closing` is set, rejecting new work during shutdown.

### Telemetry payload shape

Typical fields sent to `on_telemetry`:

- `source` — `stt` or `sr`
- `status` — `played`, `interrupted`, `dropped`, `suppressed`
- `play_started_at`, `play_ended_at`
- `actual_play_duration_ms`
- `total_buffered_ms`
- `interrupted`, `interrupted_by`
- `uid`
- `text`
- `translated`
- `translate_time`
- `tts_time`
- `play_at`

## Audio Chunk Format

`_push_audio()` splits incoming PCM bytes into exact 320-byte chunks:
- If the last chunk is short, it's zero-padded to 320 bytes
- Chunks are appended to `_audio_buf` under `_buf_lock`
- The interrupt flag is checked under the same lock to prevent pushing after interrupt

## Atmosphere Mixing

When `--atmosphere` is provided, stadium crowd noise is mixed into the audio output:

- `set_atmosphere(pcm_bytes)`: loads raw PCM into `_atmosphere_pcm`
- `set_atmosphere_enabled(bool)`: toggles mixing on/off, syncs position to video time
- `_mix_atmosphere_chunk(chunk)`: per-sample S16LE addition with volume scaling and int16 clamping

Mixing happens in two places within `_pipe_writer`:
1. **During TTS/SR playback**: atmosphere is mixed into each chunk before writing
2. **During idle (no TTS/SR)**: atmosphere-only chunks are written at 10ms rate, paced by `atmos_tick`

The atmosphere track loops: when `_atmosphere_pos` reaches the end, it wraps to the start. Position is tracked under `_atmosphere_lock` for thread safety.

Volume is 0.5x (Mel-Band Roformer output has reasonable amplitude).

## Original Audio Pass-Through

When the "Original" toggle is enabled, the pipe writer plays the source English commentary audio synced to video instead of TTS output:

- `set_original_audio(pcm_bytes)`: loads raw PCM from `--audio` file
- `set_original_enabled(bool)`: toggles on/off, syncs position to video time, disables atmosphere
- `_get_original_chunk()`: returns next 320-byte chunk, advancing position

When `_original_on` is True, `_pipe_writer`'s idle loop writes original audio chunks at 10ms rate and `continue`s past the TTS/SR check. STT and translation still run in background — queued utterances are ignored but resume naturally when original is toggled off.
