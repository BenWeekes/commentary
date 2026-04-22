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

## Multi-Session Architecture

The server uses a session-based model. Each viewer gets an isolated pipeline:

```
Viewer opens page
  → POST /api/session → backend creates session:
      - sessionId (uuid)
      - channel = "commentary-{uuid[:8]}"
      - viewer token (v007, generated via tokens.py)
      - lang file = /tmp/commentary_lang_{sessionId}
  → Returns {sessionId, channel, token, appid}
  → Viewer joins Agora channel with returned token

Viewer clicks Start
  → POST /api/session/{id}/start → spawns pipeline for this session

Viewer changes language
  → GET /api/session/{id}/set-lang?lang=fr → writes to session's lang file

Viewer clicks Stop
  → POST /api/session/{id}/stop → kills session's pipeline
```

Multiple viewers can run concurrently with different languages. Each session has its own Agora channel, Go publisher process, TTS engine, and pipeline threads.

## Threading Model (per session)

| Thread | Role |
|---|---|
| Main thread | argparse, setup, HTTP server on port 8090 |
| Pipeline thread | Per-session: runs `asyncio` event loop via `run_pipeline()` |
| STT pipeline | Deepgram WebSocket + audio feeder thread |
| SR events | Sequential event replay thread (non-daemon, joins before cleanup) |
| TTS worker | Processes text queue → ElevenLabs WebSocket → audio buffer |
| Pipe writer | Drains audio buffer at 10ms rate → Go publisher stdin |
| Publisher log | 2 threads reading Go publisher stdout/stderr |

## Component Diagram

```
viewer.html ──────────────────────────────────────┐
  │ POST /api/session                              │ Agora Web SDK
  │ POST /api/session/{id}/start                   │ (subscribe)
  │ GET  /api/session/{id}/set-lang?lang=XX        │
  ▼                                                ▼
ControlHandler (port 8090) ── SessionManager
  │                              │
  │    ┌─────────────────────────┼─────────────┐
  │    │ Session A               │ Session B    │
  │    │ channel: commentary-abc │ comm-xyz     │
  │    │ lang: es                │ lang: fr     │
  │    ▼                         ▼              │
  │  TTSEngine ── Go pub ── Agora ch A          │
  │  TTSEngine ── Go pub ── Agora ch B          │
  │                                             │
  ├── Deepgram STT (per session)                │
  └── Events file reader (per session)          │
                                                │
  ElevenLabs API ◀─── TTS engines ─────────────┘
```
