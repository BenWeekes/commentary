# L1 — Architecture

## Pipeline Overview

```
┌─────────────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐
│ Audio source │──▶ │ Deepgram │──▶ │ Correct  │──▶ │ Translate │
│ (mic/file)  │    │ Nova-3   │    │ (determ.) │    │ GPT-4o-m  │
└─────────────┘    └──────────┘    └──────────┘    └─────┬─────┘
                                                         │
┌─────────────┐    ┌──────────┐                          │
│ Sportradar  │──▶ │ Translate│──────────────────────────┤
│ events file │    │ GPT-4o-m │                          │
└─────────────┘    └──────────┘                          ▼
                                                  ┌──────────────┐
                                                  │ ElevenLabs   │
                                                  │ WebSocket TTS│
                                                  │ (pcm_16000)  │
                                                  └──────┬───────┘
                                                         │ PCM bytes
┌─────────────┐                                          ▼
│ Video file  │──▶ Go publisher ◀── PCM via stdin ──▶ Agora channel
│ (.h264)     │    (UID 73, 3s delayed video + TTS audio)
└─────────────┘
```

## 3-Second Delay Strategy

| Component | Budget |
|---|---|
| Deepgram STT | ~0.8s |
| Deterministic corrections | <1ms |
| GPT-4o-mini translation | ~0.8s |
| ElevenLabs TTS buffering | ~0.5s |
| Safety margin | ~0.9s |
| **Total** | **≤ 3.0s** |

Video is delayed 3 seconds before publishing. This gives the entire STT → translate → TTS chain time to produce audio that syncs with the corresponding video moment.

## Dual Input Model

`live_match.py` supports two concurrent commentary sources:

1. **STT pipeline** (`--audio`): Live audio → Deepgram → corrections → translate → TTS
2. **Events fallback** (`--events`): Pre-timed Sportradar events → translate → TTS

Both feed the same TTSEngine queue. Events are scheduled to play at `match_time + video_delay`.

## Threading Model

| Thread | Role |
|---|---|
| Main thread | argparse, setup, runs `asyncio` event loop via `run_pipeline()` |
| Control server | HTTP daemon on port 8090 — `/set-lang`, `/start`, `/stop`, `/status` |
| STT pipeline | Deepgram WebSocket + audio feeder thread |
| SR events | Sequential event replay thread |
| TTS worker | Processes text queue → ElevenLabs WebSocket → audio buffer |
| Pipe writer | Drains audio buffer at 10ms rate → Go publisher stdin |
| Publisher log | 2 threads reading Go publisher stdout/stderr |

## Component Diagram

```
viewer.html ──────────────────────────────────┐
  │ /set-lang, /start, /stop                  │ Agora Web SDK
  ▼                                           │ (subscribe)
ControlHandler (port 8090)                    │
  │                                           │
  ▼                                           ▼
live_match.py ── TTSEngine ── Go publisher ── Agora channel
  │                │                 │
  │                ▼                 ▼
  │           ElevenLabs API    H.264 video file
  │
  ├── Deepgram STT (WebSocket)
  └── Events file reader
```
