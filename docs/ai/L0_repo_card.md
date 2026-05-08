# Commentary — Repo Card

> Real-time multilingual soccer commentary via STT → translate → TTS → Agora.

## Identity

| Field | Value |
|---|---|
| Repo | `commentary` |
| Type | `application` |
| Language | Python 3.10+, Go 1.21+ (video publisher) |
| Deploy Target | local / demo / production server |
| Owner | benweekes |
| Last Reviewed | 2026-05-08 |

## L1 — Summaries

| File | Purpose | Audience |
|---|---|---|
| [01_setup](L1/01_setup.md) | Prerequisites, env vars, install, server mode setup, Go publisher setup | Use & Maintain |
| [02_architecture](L1/02_architecture.md) | Server mode, demo/live match modes, timing model, multi-language pipeline | Maintain |
| [03_code_map](L1/03_code_map.md) | Directory tree, module maps for `server/`, `lib/`, all scripts | Maintain |
| [04_conventions](L1/04_conventions.md) | Naming, voices, JIT translation, audio format, logging, YAML config | Maintain |
| [05_workflows](L1/05_workflows.md) | Server mode, dev mode, clip extraction, viewer usage, benchmarks | Use |
| [06_interfaces](L1/06_interfaces.md) | Production server API, Agora channel contracts, PCM format, live mode contracts | Use & Maintain |
| [07_gotchas](L1/07_gotchas.md) | Server port conflicts, YAML paths, MP4 offset, zombies, TTS short phrases, go.mod replace | Maintain |
| [08_security](L1/08_security.md) | API keys, tokens, CORS, network exposure, server security gaps | Maintain |

## L2 — Deep Dives

| File | Topic |
|---|---|
| [tts_engine](L1/L2/tts_engine.md) | TTSEngine threading, buffer, pipe writer (`lib/tts_engine.py`) |
| [stt_pipeline](L1/L2/stt_pipeline.md) | Deepgram → corrections → multi-lang fan-out pipeline (`lib/stt_pipeline.py`) |
| [tts_timeline_format](L1/L2/tts_timeline_format.md) | TTS playback log analysis and timing verification |
