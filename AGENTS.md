# Commentary — AI Agent Entry Point

Real-time multilingual soccer commentary system: STT → translate → TTS → Agora.

## Loading Protocol

1. **Always read first**: `docs/ai/L0_repo_card.md` — identity, tech stack, L1 index
2. **Then read all L1 files**: `docs/ai/L1/01_setup.md` through `docs/ai/L1/08_security.md`
3. **Read L2 on demand**: deep dives in `docs/ai/L1/deep_dives/` — only when working on TTS engine internals or STT pipeline

## Quick Reference

| What | Where |
|---|---|
| Main orchestrator | `live_match.py` |
| Architecture overview | `docs/ai/L1/02_architecture.md` |
| File/module map | `docs/ai/L1/03_code_map.md` |
| Run modes and workflows | `docs/ai/L1/05_workflows.md` |
| API keys and env vars | `docs/ai/L1/01_setup.md` |
| Known gotchas | `docs/ai/L1/07_gotchas.md` |
| TTS engine internals | `docs/ai/L1/deep_dives/tts_engine.md` |
| STT pipeline details | `docs/ai/L1/deep_dives/stt_pipeline.md` |

## Conventions

- Python 3.10+, no type annotations in existing code
- `.env` file loaded by `_load_dotenv()` in `live_match.py` — never commit `.env`
- Events format: `offset_seconds|PRIORITY|message text`
- PCM audio: 16-bit signed LE, 16 kHz, mono, 10ms chunks (320 bytes)
- Data files live in `data/events/`, `data/audio/`, `data/json/`
