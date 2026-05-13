# L2 — TTSEngine Internals

> **When to Read This:** You are modifying the TTSEngine class, debugging audio playback or buffering issues, changing the atmosphere mixing logic, or investigating pipe writer timing.

## Overview

`TTSEngine` (`lib/tts_engine.py`) manages ElevenLabs WebSocket TTS and PCM audio delivery to the Go publisher.

## Threading Architecture

```
speak() ──▶ _text_queue ──▶ _tts_worker coordinator ──▶ _ready_heap ──▶ _audio_buf ──▶ _pipe_writer ──▶ stdin pipe
   │              │                  │                       │              │
   │         Queue()                 │                    heapq by       deque()
   │                                 │                    play_at        10ms timer
   │                                 ▼
   │                         _prepare_executor
   │                         (2 threads per language:
   │                          translate + ElevenLabs TTS
   │                          into local PCM bytes)
   └── interrupt=True clears queue + ready heap; in-flight prepare may finish and be discarded
```

### Parallel prepare executor

`_prepare_executor = ThreadPoolExecutor(max_workers=2)` is owned by each `TTSEngine` instance. When STT queues a new item, the coordinator submits translate+TTS work as soon as a prepare slot is available. The task:

- calls `translate_fn(text)` if needed
- opens an ElevenLabs WebSocket
- collects the whole utterance into local PCM bytes
- returns result metadata plus `pcm_bytes`

This is intentionally bounded at two in-flight prepare tasks per language to avoid excessive ElevenLabs concurrency. In-flight tasks are not forcibly cancelled on interruption; if they finish stale, their prepared audio is discarded and `discarded_ms` is logged.

For live STT, translation may race the configured primary model against a fast fallback before TTS starts. The prepared result carries `translation_model_used` and `translation_fallback_reason` through to playback telemetry, including dropped/replaced items, so deadline misses can be separated from fallback wins.

The older `_pretranslate_executor` cache remains present for compatibility but the live STT path now relies on full parallel preparation rather than translation-only lookahead.

### _tts_worker coordinator

The `_tts_worker` thread no longer performs translation/TTS itself. It coordinates:

1. submit queued STT items into `_prepare_executor` while fewer than two are in flight
2. move completed prepare futures into `_ready_heap`, keyed by `play_at`
3. select the earliest ready item
4. hold until `play_at`, or drop if it is more than the late-start grace behind
5. apply local ffmpeg `atempo` gap fitting against the next known STT `play_at`
6. move local PCM bytes into `_audio_buf`, set `_playback_meta_slot`, and signal `_playback_ready`

Order-of-completion is not order-of-play: a later short utterance may finish TTS first, but it stays in `_ready_heap` until earlier `play_at` items have either played or been dropped.

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
Old serial worker:
  t0 ── xlat+TTS ── t1 ── wait ── t2 ── playback ── t3 ── xlat+TTS ── t4 ── wait ── t5
                                                      │                               │
                                                      └── next item blocked until here

Parallel prepare, ordered play:
  Queue: [item1, item2, item3]
  Prep A: xlat+TTS(item1) ── ready heap ── play_at(item1)
  Prep B: xlat+TTS(item2) ── ready heap ── play_at(item2)
          xlat+TTS(item3) ── ready heap ── play_at(item3)
```

Parallel prepare removes avoidable queue wait during STT bursts. It does not help if STT itself emits an utterance too late for the configured `video_delay` budget.

## Speed Fitting

ElevenLabs is called with `speed=1.0`, `stability=1.0`, and `similarity_boost=1.0`. The engine keeps the voice stable and performs dynamic speed changes locally after audio is generated.

When an STT item has a later STT item queued, `_fit_current_audio_to_next_play_at()` compares the generated PCM duration with the available window before the next STT `play_at`, minus a small guard. If the clip differs by more than 5%, it runs ffmpeg `atempo` on the PCM:

- pitch is preserved by `atempo`
- factors above `2.0x` are chained as multiple `atempo` filters
- the factor is clamped to `0.769x` slow-down through `1.3x` speed-up
- SR events are not used as the fit deadline; the fit target is the next STT item
- provider word spans are logged but are not used as hard target durations because they can describe acoustic onset differently from perceived TTS duration

The result metadata includes `local_speed_factor`, `fit_from_ms`, `fit_to_ms`, `fit_deadline_ms`, `fit_cpu_ms`, and `fit_reason`, which are written to per-language JSONL logs.

For Soniox forced-split continuations, the engine also supports explicit split chaining. It does not infer continuity from grammar. If item N and item N+1 share `split_group_id` and adjacent `split_part_index`, item N+1 can start 30ms after item N ends when that is earlier than the original source-timed `play_at`. The previous split playback state is cleared by any intervening non-split STT playback, and the timing advance is capped at 4000ms. The log records `original_play_at`, `split_chain_gap_ms`, and `split_chain_advance_ms`.

Normal STT utterances can also use conservative continuity chaining. This only applies when the previous STT playback completed normally, both utterances have the same speaker, the source audio gap is `0-900ms`, and the timing advance would be at most `1500ms`. In that case item N+1 can start 100ms after item N ends. The log records `continuity_chain_source_gap_ms`, `continuity_chain_gap_ms`, and `continuity_chain_advance_ms`.

This continuity chain is intentionally source-timing based, not grammar based. It does not inspect phrases such as "going to be"; the guardrails are speaker identity, source gap, successful previous playback, and maximum advance. If the required advance exceeds `1500ms`, the item remains on its original source-timed schedule. That cap keeps the behavior auditable and avoids collapsing genuinely separate commentary beats.

## Scheduling

`speak(text, play_at=timestamp)` schedules playback to start at a specific wall-clock time. The prepare executor fetches audio immediately, and the coordinator holds the prepared result until `play_at`. This is used by live STT and by the events fallback to sync commentary with delayed video:

```python
play_at = match_time_start + event_offset
```

### Precision targeting

Utterances must play at exact play_at time or be dropped. The hold uses a two-phase approach for sub-10ms accuracy:
1. **Coarse sleep**: short sleeps until 50ms before target
2. **Tight spin**: busy-wait `while time.time() < play_at` — hits ±1ms

The pipe writer blocks on `threading.Event.wait()` instead of polling, so it wakes within microseconds of `_playback_ready.set()`. Combined, the total chain from `play_at` to first PCM byte on stdin is <5ms.

## Interrupt Flow

1. `speak(text, interrupt=True)` is called
2. `_interrupt` event is set
3. `_audio_buf`, `_sr_audio_buf`, and the ready heap are cleared
4. `_text_queue` is drained
5. New text is queued
6. In-flight prepare tasks are allowed to finish; if their generation is stale they are emitted as `replaced` with `discarded_ms`
7. `_interrupt` is cleared when the next item starts playback

## State Tracking

- `is_speaking` event: set when the coordinator has queued, in-flight, ready, or active STT work; cleared when empty
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

1. `_prepare_executor` processes an STT item into local PCM bytes + result metadata
2. `_tts_worker` moves the selected result's PCM chunks into `_audio_buf`
3. Before signaling `_playback_ready`, it sets `_playback_meta_slot = result` under `_buf_lock`
4. `_pipe_writer` captures the slot into `current_meta` under lock at the top of the playback cycle (before checking `n_chunks`), then clears the slot
5. After playback ends, `_pipe_writer` uses `current_meta` to emit one telemetry record via `on_telemetry`
6. Dropped/suppressed items are queued into `_skipped_meta` and emitted from `_pipe_writer` so telemetry stays single-threaded from the consumer side

### Status semantics

- `played` — utterance started and completed normally
- `interrupted` — playback started but was cut short (only set by `_pipe_writer` when `_interrupt` is detected during active playback)
- `dropped` — item never started playback (cleared from queue by `speak(interrupt=True)`, TTS returned no audio, or shutdown)
- `replaced` — queued STT item never started because a fresher STT item took the slot
- `suppressed` — STT utterance was discarded because SR was already occupying the slot

Items that never played are `dropped`, `replaced`, or `suppressed`, not `interrupted`. Only `_pipe_writer` can set `interrupted` — it detects `_interrupt.is_set()` during active chunk drain.

## Shutdown

`stop()` uses a two-phase approach to prevent final telemetry loss:

1. **Phase 0 (executor)**: Shuts down `_pretranslate_executor` and `_prepare_executor` with `wait=False` to stop pending work.
2. **Phase 1 (closing)**: Sets `_closing = True`, sets `_interrupt` to wake `_tts_worker`. Joins `_tts_worker_thread` (timeout 2s). The worker finishes any in-flight item, writes the final slot, then exits on empty queue + `_closing`.
3. **Phase 2 (stopped)**: Sets `_stop`, wakes `_pipe_writer` via `_any_playback_ready`. Joins `_pipe_writer_thread` (timeout 1s). The writer drains any final slot and `_skipped_meta`, then exits.

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
- `pre_translated` — legacy translation-cache flag; usually `false` in the current parallel prepare path
- `queue_wait_ms` — milliseconds the item spent waiting before a prepare worker started it
- `local_speed_factor`, `fit_from_ms`, `fit_to_ms`, `fit_deadline_ms`, `fit_cpu_ms`, `fit_reason` — local ffmpeg speed-fitting telemetry
- `prepare_started_at`, `translate_started_at`, `translate_ended_at`, `tts_started_at`, `tts_ended_at`, `ready_at` — absolute per-stage wall-clock timestamps
- `discarded_ms` — prepared audio duration abandoned because an item was replaced or suppressed

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
