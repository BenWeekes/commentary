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
| `openai` | >=1.0.0 | GPT-5.4 / GPT-4o-mini translation API |
| `websockets` | >=12.0 | ElevenLabs TTS WebSocket client |
| `deepgram-sdk` | >=3.0.0 | Deepgram Nova-3 STT |
| `pyyaml` | >=6.0 | Server mode YAML config parsing |

No other Python packages are required. Standard library modules (`asyncio`, `threading`, `wave`, `struct`, `http.server`, `subprocess`, `json`, `hashlib`, `hmac`, `zlib`) handle the rest.

## Environment Variables

| Variable | Service | Required by |
|---|---|---|
| `OPENAI_API_KEY` | GPT translation (4o-mini / 5.4-mini) | All scripts |
| `DEEPGRAM_API_KEY` | Nova-3 STT | `live_match.py --audio`, `stt_realtime_translate.py`, server mode |
| `ELEVENLABS_API_KEY` | WebSocket TTS | `live_match.py`, server mode |
| `AGORA_APP_ID` | Agora channel | `live_match.py` with video, server mode |
| `AGORA_APP_CERT` | Token generation | `live_match.py` with video, server mode, `tokens.py` |
| `SPORTRADAR_API_KEY` | Soccer Extended API | `commentary_feeder.py`, `generate_demo_transcript.py`, server mode (roster fetch) |

## Optional env vars

| Variable | Default | Purpose |
|---|---|---|
| `ELEVENLABS_VOICE_ID` | `7fGUbxDMrefqPDjc4Anc` | Default TTS voice |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | ElevenLabs model |

## .env loading

Both `live_match.py` and `server/main.py` load `.env` via `_load_dotenv()` at import time. Other scripts read env vars directly or accept them as CLI args. The `.env` file must be in the repo root.

## Server Mode Setup

The production server uses a YAML config file (`matches.yaml`) instead of CLI args.

### Config file

```yaml
control_port: 8080
translation_model: "gpt-5.4"

matches:
  - match_id: bmg_fch_demo
    sport_event_id: "sr:sport_event:61514104"
    audio: clips/bmg_fch_demo_5min/audio.mp3
    video_h264: clips/bmg_fch_demo_5min/video.h264
    events: clips/bmg_fch_demo_5min/events.txt
    atmosphere: clips/bmg_fch_demo_5min/atmosphere.wav
    video_delay: 7.0
    languages: [es, pt, fr, tr, de]
```

File paths in `matches.yaml` are resolved relative to the config file's directory.

### Live match config

Live matches use a separate config file `matches_live.yaml`. Current SRT matches use `source.type = srt_direct`, which pulls the remote SRT feed directly because the media-gateway path does not handle this source cleanly enough for RTC output. The direct ingester decodes selected commentary and atmosphere tracks to local PCM, repacketizes SRT H.264 into Agora-friendly access units, and publishes a buffered original channel. For existing Agora-backed live matches, legacy flat fields still work during migration. See [05_workflows.md](05_workflows.md) for details.

### Validate config (dry run)

```bash
python3 -m server.main --config matches.yaml --dry-run
```

Validates all API keys are set, all referenced files exist, and each match has at least one language configured.

### Start server

```bash
python3 -m server.main --config matches.yaml
```

Matches stay idle until started via `POST /api/matches/{id}/start`. See [05_workflows.md](05_workflows.md) for the full operational workflow.

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

### Server mode (recommended)

```bash
python3 -m server.main --config matches.yaml --dry-run
```

If validation passes, all file paths and API keys are correctly configured.

### Standalone live pipeline test

Use `test_live_pipeline.py` to verify the live media path without starting the full match server flow. It loads API keys from `.env`, subscribes to a live source channel, runs STT → translate → TTS for one language, and publishes the result into a separate output channel.

Minimal example:

```bash
python3 test_live_pipeline.py \
    --source-channel bvb_sge_md33 \
    --output-channel bvb_sge_md33-es-test \
    --lang es \
    --video-delay 10 \
    --match-id bvb_sge_md33 \
    --sport-event-id sr:sport_event:61514184
```

Viewer-friendly test setup:

```bash
python3 test_live_pipeline.py \
    --lang es \
    --test-id e2e01 \
    --video-delay 10 \
    --sport-event-id sr:sport_event:61514184 \
    --write-test-config matches_live_test.yaml \
    --prepare-only
```

This derives:

- `match_id = livepipe_e2e01`
- `source_channel = livepipe_e2e01_src`
- `output_channel = livepipe_e2e01-es`

and writes `matches_live_test.yaml` for the existing `viewer_live.html` flow.

### Dev mode — Minimal test (no video, no Deepgram)

```bash
python3 live_match.py \
    --events clips/bmg_fch_demo_5min/events.txt \
    --lang es
```

This only requires `OPENAI_API_KEY` and `ELEVENLABS_API_KEY`. It replays pre-timed events through TTS without video or STT.

### Dev mode — Full test (video + STT + events + atmosphere)

```bash
python3 live_match.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --video-h264 clips/bmg_fch_demo_5min/video.h264 \
    --events clips/bmg_fch_demo_5min/events.txt \
    --atmosphere clips/bmg_fch_demo_5min/atmosphere.wav \
    --lang es --video-delay 7
```

Requires all 5 core API keys plus the Go publisher binary. Open `http://localhost:8090/viewer.html` to test.

## Related Deep Dives

- [09_deployment.md](09_deployment.md) — Ubuntu server deployment (systemd, nginx, TLS, firewall)
