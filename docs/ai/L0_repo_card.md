# Commentary — Repo Card

> Real-time multilingual soccer commentary via STT → translate → TTS → Agora.

## Identity

| Field | Value |
|---|---|
| Repo | `commentary` |
| Type | `application` |
| Language | Python 3.10+, Go 1.21+ (video publisher) |
| Deploy Target | local / demo |
| Owner | benweekes |
| Last Reviewed | 2026-04-29 |

## L1 — Summaries

| File | Purpose | Audience |
|---|---|---|
| [01_setup](L1/01_setup.md) | Prerequisites, env vars, install, Go publisher setup | Use & Maintain |
| [02_architecture](L1/02_architecture.md) | 3s delay pipeline, timing model, multi-session architecture | Maintain |
| [03_code_map](L1/03_code_map.md) | Directory tree, module maps for all scripts | Maintain |
| [04_conventions](L1/04_conventions.md) | Naming, voices, JIT translation, audio format, logging | Maintain |
| [05_workflows](L1/05_workflows.md) | Run modes, clip extraction, multi-session viewer, benchmarks | Use |
| [06_interfaces](L1/06_interfaces.md) | Session API, Agora contract, PCM format, events format | Use & Maintain |
| [07_gotchas](L1/07_gotchas.md) | MP4 kickoff offset, zombies, TTS short phrases, go.mod replace | Maintain |
| [08_security](L1/08_security.md) | API keys, tokens, CORS, network exposure, subprocess isolation | Maintain |

## L2 — Deep Dives

| File | Topic |
|---|---|
| [tts_engine](L1/L2/tts_engine.md) | TTSEngine threading, buffer, pipe writer |
| [stt_pipeline](L1/L2/stt_pipeline.md) | Deepgram → corrections → translation pipeline |
| [tts_timeline_format](L1/L2/tts_timeline_format.md) | TTS playback log analysis and timing verification |
