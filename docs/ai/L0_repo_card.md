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
| Last Reviewed | 2026-07-26 |

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
| [09_deployment](L1/09_deployment.md) | Ubuntu server deployment: systemd, nginx, TLS, firewall, logs | Use & Maintain |
| [10_experiments](L1/10_experiments.md) | Side experiments index: **AI live commentator (rounds 1–10, current)**, V2V provider comparison, BWE/ABR feasibility — headline results + artefact locations | Use & Maintain |

## L2 — Deep Dives

| File | Topic |
|---|---|
| [blend_pipeline](L2/blend_pipeline.md) | **AI Football Commentator (experiments/ai_commentator)** — pipeline & HITL process; v7 accepted, PARKED 2026-08-04 |
| [hitl_tuning_workflow](L2/hitl_tuning_workflow.md) | Review → distill → gate improvement loop; rule ledger + round history |
| [tennis_pipeline](L2/tennis_pipeline.md) | Isolated Glinka–Mayo tennis commentator: score tracking, STT guards, timing gate, review lifecycle |
| [review_cycle_1_dispositions](L2/review_cycle_1_dispositions.md) | Per-comment audit of review cycle 1 |
| [resolution_tracker_eval](L2/resolution_tracker_eval.md) | Resolution/tracker evaluation notes |
| [tts_engine](L2/tts_engine.md) | TTSEngine threading, buffer, pipe writer (`lib/tts_engine.py`) |
| [stt_pipeline](L2/stt_pipeline.md) | Deepgram → corrections → multi-lang fan-out pipeline (`lib/stt_pipeline.py`) |
| [tts_timeline_format](L2/tts_timeline_format.md) | TTS playback log analysis and timing verification |
