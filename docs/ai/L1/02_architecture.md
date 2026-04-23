# L1 — Architecture

## What We're Building

A live commentary translation service for live football matches. Two audio sources feed translated commentary to viewers via Agora:

1. **SR (Sportradar) AI commentary** — arrives via SR websocket with match timestamps
2. **STT (live game audio)** — original commentator's speech, transcribed via Deepgram

Both are translated and spoken via TTS, synced to delayed video so the viewer hears translated commentary at the exact moment the original was spoken.

## Timing Model

```
The Go publisher delays video by --video-delay seconds (default 7s).
The STT audio feed starts immediately, giving translations a head start.

For STT utterances:
  play_at = video_start + audio_start

  video_start = wall time when Go publisher finishes the delay and sends first frame
  audio_start = when the commentator spoke (from Deepgram)

  Since the audio feed started video_delay seconds before video_start,
  translations are typically ready ~1-2s before play_at.

For SR events:
  play_at = match_time_start + event_offset
  Prefetched — TTS is ready seconds before play_at. Always ±0ms.

Rule: play within ±100ms of play_at, or drop the utterance.
```

## Pipeline Overview

```
┌──────────────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐
│ Live audio   │──▶ │ Deepgram │──▶ │ Correct  │──▶ │ Translate │
│ (mic/file)   │    │ Nova-3   │    │ (determ.) │    │ GPT-4o-m  │
└──────────────┘    └──────────┘    └──────────┘    └─────┬─────┘
                    endpointing=200                       │
                    utterance_end_ms=1000                  │
┌──────────────┐    ┌──────────┐                          │
│ SR websocket │──▶ │ Translate│──────────────────────────┤
│ (live/file)  │    │ GPT-4o-m │                          │
└──────────────┘    └──────────┘                          ▼
                                                    ┌──────────────┐
                                                    │ ElevenLabs   │
                                                    │ WebSocket TTS│
                                                    │ (pcm_16000)  │
                                                    └──────┬───────┘
                                                           │ PCM bytes
┌──────────────┐                                           ▼
│ Live video   │──▶ Go publisher ◀── PCM via stdin ──▶ Agora channel
│ (delayed 7s) │    (starts audio immediately, delays video)
└──────────────┘
```

## Startup Sequence

1. Go publisher connects to Agora, starts reading audio from stdin immediately
2. STT pipeline starts — audio feed begins, Deepgram processes in real-time
3. Go publisher sleeps `video_delay` seconds (video frames held back)
4. After delay, publisher starts sending video → `video_start` is set
5. Translations from step 2 are already ready → play in sync with video

## Playback Rules

- **SR events**: prefetched TTS, scheduled to exact match time. Always ±0ms.
- **STT utterances**: translated + TTS'd as fast as possible. If ready within ±100ms of play_at, hold and play at exact time. If >100ms late, drop — the moment has passed.
- **SR INTERRUPT** (e.g. GOAL): clears STT queue, plays to completion uninterrupted.
- **STT can interrupt SR APPEND**: if STT audio is ready while SR is playing, STT takes priority (pipe_writer prefers STT buffer). STT plays to completion — the original commentator fit it in, so the translated TTS (which is shorter) will too.
- **Queue stays at 0-1**: when a new STT utterance arrives with play_at, any stale queued item is replaced.

## Multi-Session Architecture

Each viewer gets an isolated pipeline:

```
POST /api/session       → creates session (channel, token, lang file)
GET  /session/{id}/start → spawns pipeline: Go publisher + TTS + STT + SR
GET  /session/{id}/set-lang?lang=fr → writes to session's lang file
POST /session/{id}/stop  → kills pipeline
```

Multiple viewers run concurrently with different languages. Each has its own Agora channel, Go publisher, TTS engine, and pipeline threads.

## Key Parameters

| Parameter | Default | Effect |
|---|---|---|
| `--video-delay` | 7.0s | Pipeline budget. Longer = more STT utterances survive |
| `--events-offset` | 0 | Match-time offset for events replay |
| `--lang` | es | Default translation language |
| `endpointing` | 200ms | Deepgram VAD — shorter = faster turn detection |
| `utterance_end_ms` | 1000ms | Deepgram utterance boundary (minimum 1000ms) |
