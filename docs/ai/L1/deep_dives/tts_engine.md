# L2 — TTSEngine Internals

## Overview

`TTSEngine` (`live_match.py:330–592`) manages ElevenLabs WebSocket TTS and PCM audio delivery to the Go publisher.

## Threading Architecture

```
speak() ──▶ _text_queue ──▶ _tts_worker thread ──▶ _audio_buf ──▶ _pipe_writer thread ──▶ stdin pipe
   │              │                │                     │                │
   │         Queue()           asyncio loop          deque()         10ms timer
   │                          (per thread)        (thread-safe)
   └── interrupt=True clears queue + buffer
```

### _tts_worker thread

- Runs its own `asyncio` event loop (`asyncio.new_event_loop()`)
- Processes `_text_queue` items one at a time
- For each item:
  1. Calls `translate_fn(text)` if provided (JIT translation)
  2. Connects to ElevenLabs WebSocket
  3. Sends text, receives base64-encoded PCM audio chunks
  4. Pushes all audio into `_audio_buf` via `_push_audio()`
  5. Sets `_playback_ready` event when all audio is received (full pre-buffer)
  6. Waits for `play_at` time if scheduled
  7. Waits for pipe writer to drain the buffer

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
Timeline for one utterance:
  t0 ──── TTS download ────── t1 ── wait for play_at ── t2 ──── playback ──── t3
  │                            │                          │                     │
  └── _tts() async method      └── _playback_ready set   └── pipe writer       └── buffer empty
                                                              starts draining
```

## Scheduling

`speak(text, play_at=timestamp)` schedules playback to start at a specific wall-clock time. The TTS worker fetches audio immediately but holds playback until `play_at`. This is used by the events fallback to sync commentary with delayed video:

```python
play_at = match_time_start + event_offset + video_delay
```

### Precision targeting (±2s)

Utterances within 2s of play_at are played (slightly late is better than dropped). The hold uses a two-phase approach for sub-10ms accuracy:
1. **Coarse sleep**: `time.sleep(wait_s - 0.05)` — sleeps until 50ms before target
2. **Tight spin**: busy-wait `while time.time() < play_at` — hits ±1ms

The pipe writer blocks on `threading.Event.wait()` instead of polling, so it wakes within microseconds of `_playback_ready.set()`. Combined, the total chain from `play_at` to first PCM byte on stdin is <5ms.

## Interrupt Flow

1. `speak(text, interrupt=True)` is called
2. `_interrupt` event is set
3. `_audio_buf` is cleared (under lock)
4. `_text_queue` is drained
5. New text is queued
6. `_tts_worker` checks `_interrupt` before and after TTS — skips if set
7. `_interrupt` is cleared when the next non-interrupt item starts

## State Tracking

- `is_speaking` event: set when TTS worker is processing, cleared when queue empties
- `on_idle` callback: called when queue empties (used for external coordination)
- `_utterance_id`: monotonically increasing counter for log correlation

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
