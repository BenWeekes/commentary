# L1 — Setup

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.10+ | All scripts |
| ffmpeg | any | Audio conversion (mp3 → PCM WAV) |
| Go | 1.21+ | Video publisher (optional) |
| Agora SDK | macOS/Linux native | Go publisher's CGo dependency (optional) |

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in API keys
```

## Environment Variables

| Variable | Service | Required by |
|---|---|---|
| `OPENAI_API_KEY` | GPT-4o-mini translation | All scripts |
| `DEEPGRAM_API_KEY` | Nova-3 STT | `live_match.py --audio`, `stt_realtime_translate.py` |
| `ELEVENLABS_API_KEY` | WebSocket TTS | `live_match.py` |
| `AGORA_APP_ID` | Agora channel | `live_match.py` with video |
| `AGORA_APP_CERT` | Token generation | `live_match.py` with video, `tokens.py` |
| `SPORTRADAR_API_KEY` | Soccer Extended API | `commentary_feeder.py` |

## Optional env vars

| Variable | Default | Purpose |
|---|---|---|
| `ELEVENLABS_VOICE_ID` | `7fGUbxDMrefqPDjc4Anc` | Default TTS voice |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | ElevenLabs model |

## .env loading

`live_match.py` loads `.env` via `_load_dotenv()` at import time. Other scripts read env vars directly or accept them as CLI args. The `.env` file must be in the same directory as `live_match.py`.

## Go publisher setup

See `go-audio-video-publisher/README.md`. Key steps:

1. Install the Agora Go Server SDK locally
2. Update `go.mod` line 10 — change the `replace` directive to your SDK path
3. Set `DYLD_LIBRARY_PATH` to the SDK's native library directory
4. Install ffmpeg dev libraries (`brew install ffmpeg` on macOS)
