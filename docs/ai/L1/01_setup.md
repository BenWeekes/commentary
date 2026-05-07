# L1 — Setup

> Everything needed to clone, configure, and run the project.

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.10+ | All scripts |
| ffmpeg | any | Audio conversion (mp3 → PCM WAV) |
| Go | 1.21+ | Video publisher (optional) |
| Agora SDK | macOS/Linux native | Go publisher's CGo dependency (optional) |

## Install

```bash
git clone <repo-url> && cd commentary
pip install -r requirements.txt
cp .env.example .env
# fill in API keys (see Environment Variables below)
```

### Python dependencies

| Package | Version | Purpose |
|---|---|---|
| `openai` | >=1.0.0 | GPT-4o-mini translation API |
| `websockets` | >=12.0 | ElevenLabs TTS WebSocket client |
| `deepgram-sdk` | >=3.0.0 | Deepgram Nova-3 STT |

No other Python packages are required. Standard library modules (`asyncio`, `threading`, `wave`, `struct`, `http.server`, `subprocess`, `json`, `hashlib`, `hmac`, `zlib`) handle the rest.

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

### Building the Go publisher

```bash
cd go-audio-video-publisher
make build
```

The `Makefile` runs `go build` with CGo enabled. The Agora SDK native libraries must be on `DYLD_LIBRARY_PATH` (macOS) or `LD_LIBRARY_PATH` (Linux) at both build and runtime.

## Verifying the setup

### Minimal test (no video, no Deepgram)

```bash
python3 live_match.py \
    --events data/events/bmg_fch_35_40_clip.txt \
    --lang es
```

This only requires `OPENAI_API_KEY` and `ELEVENLABS_API_KEY`. It replays pre-timed events through TTS without video or STT.

### Full test (video + STT + events)

Requires all 5 core API keys plus the Go publisher binary and an H.264 file. See `docs/ai/L1/05_workflows.md` for the full command.

## Related Deep Dives

None — setup is self-contained.
