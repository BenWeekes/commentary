# L0 — Repository Card

| Field | Value |
|---|---|
| **Name** | commentary |
| **Purpose** | Real-time multilingual soccer commentary via STT → translate → TTS → Agora |
| **Primary language** | Python 3.10+ |
| **Secondary language** | Go 1.21+ (video publisher) |
| **Key dependencies** | openai, websockets, deepgram-sdk, ElevenLabs API, Agora RTC SDK |
| **Entry point** | `live_match.py` |
| **Config** | `.env` (loaded by `_load_dotenv()`) |
| **Data** | `data/events/`, `data/audio/`, `data/json/` |
| **License** | MIT |

## L1 Index

| File | Topic |
|---|---|
| `L1/01_setup.md` | Prerequisites, env vars, install |
| `L1/02_architecture.md` | 3s delay pipeline, component diagram |
| `L1/03_code_map.md` | Directory tree, module map |
| `L1/04_conventions.md` | Naming, voices, pass filtering, JIT translation |
| `L1/05_workflows.md` | Run modes, add language, generate token |
| `L1/06_interfaces.md` | Control API, Agora contract, PCM format, events format |
| `L1/07_gotchas.md` | Zombies, go.mod replace, DYLD_LIBRARY_PATH |
| `L1/08_security.md` | API keys, tokens, CORS |

## L2 Deep Dives

| File | Topic |
|---|---|
| `L1/deep_dives/tts_engine.md` | TTSEngine threading, buffer, pipe writer |
| `L1/deep_dives/stt_pipeline.md` | Deepgram → corrections → translation pipeline |
