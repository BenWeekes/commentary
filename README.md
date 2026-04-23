# Live Multilingual Soccer Commentary

Real-time system that takes English soccer commentary audio, translates it into any of 10 languages, and broadcasts the translated speech alongside match video over an Agora channel. Video is delayed by a configurable amount (default 7s) giving the STT + translation + TTS pipeline time to produce audio that plays in sync with the video.

## How it works

```
Audio source ──▶ Deepgram STT ──▶ Corrections ──▶ GPT-4o-mini ──▶ ElevenLabs TTS ──▶ Agora channel
                  (Nova-3)         (deterministic)   (translate)     (WebSocket PCM)     (UID 73)

Sportradar events ─────────────────────────────────────────────────────┘
Match video (.h264) ───────────────────────────────────────────────────┘
```

The pipeline runs inside `live_match.py`. The Go publisher delays video by `--video-delay` seconds while the STT pipeline processes audio immediately, giving translations time to be ready before the viewer sees each moment.

## Supported languages

Spanish, French, German, Portuguese, Italian, Arabic, Japanese, Korean, Chinese, Hindi, English (passthrough)

## Prerequisites

- Python 3.10+
- ffmpeg (for audio conversion)
- Go 1.21+ (only if publishing video via the Go publisher)
- Agora Linux/macOS SDK (only if publishing video)
- API keys for: OpenAI, Deepgram, ElevenLabs, Agora, Sportradar

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/BenWeekes/commentary.git
cd commentary
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
# Edit .env with your keys

# 3. Run events-only mode (no STT, no video — easiest to test)
python3 live_match.py \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es

# 4. Run with STT audio
python3 live_match.py \
    --audio data/audio/bmg_fch_first_5min.mp3 \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es

# 5. Full demo with video + STT + events (8s video delay)
python3 live_match.py \
    --audio data/audio/bmg_fch_first_5min.mp3 \
    --video-h264 go-audio-video-publisher/encoded_assets/bundesliga.h264 \
    --events data/events/bmg_fch_md28_full_match.txt \
    --lang es --video-delay 7
```

## Viewer

The viewer is built into the server. Open `http://localhost:8090` in your browser.

1. The page creates a session automatically (each tab gets its own Agora channel)
2. Click **Start** to begin — video appears after `--video-delay` seconds
3. Use the language dropdown to switch commentary language in real time
4. Click **Stop** to end the session

## Scripts

| Script | Purpose |
|---|---|
| `live_match.py` | Main orchestrator — STT + translate + TTS + video publisher |
| `commentary_feeder.py` | Polls Sportradar API and pushes commentary to an Agora avatar agent |
| `match_replay.py` | Replays an events file through the avatar at configurable speed |
| `stt_realtime_translate.py` | STT benchmark — measures Deepgram + translation latency |
| `viewer.html` | Browser-based Agora viewer with language selector and start/stop controls |
| `tokens.py` | Agora v007 token generation (RTC + RTM) — pure stdlib, no dependencies |

## Data files

| Path | Contents |
|---|---|
| `data/events/*.txt` | Match event files (`offset\|priority\|message` format) |
| `data/audio/*.mp3` | Commentary audio samples |
| `data/json/*.json` | Full Sportradar API responses for offline development |

## Go video publisher

The `go-audio-video-publisher/` directory contains a Go program that publishes H.264 video and PCM audio to an Agora channel. See its [README](go-audio-video-publisher/README.md) for build instructions.

**Important**: The `go.mod` file contains a `replace` directive pointing to a local Agora SDK path. Update line 10 to point to your local copy of the [Agora Go Server SDK](https://github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK).

When running `live_match.py` with video, set `DYLD_LIBRARY_PATH` to your Agora SDK's native library directory:

```bash
export DYLD_LIBRARY_PATH=/path/to/agora_sdk_mac
```

## Generating H.264 video

The encoded video assets are not included in this repo. Generate your own:

```bash
ffmpeg -hide_banner -y -i match.mp4 -an \
    -vf "scale=1280:720,fps=25" \
    -pix_fmt yuv420p \
    -c:v libx264 -profile:v high -level 3.1 \
    -preset veryfast \
    -x264-params "keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1" \
    -b:v 2800k -maxrate 3200k -bufsize 6400k \
    -f h264 go-audio-video-publisher/encoded_assets/match_720p25.h264
```

## Environment variables

| Variable | Used by | Required for |
|---|---|---|
| `OPENAI_API_KEY` | Translation (GPT-4o-mini) | All scripts |
| `DEEPGRAM_API_KEY` | STT (Nova-3) | `live_match.py --audio`, `stt_realtime_translate.py` |
| `ELEVENLABS_API_KEY` | TTS (WebSocket streaming) | `live_match.py` |
| `AGORA_APP_ID` | Channel publishing | `live_match.py --video-h264` |
| `AGORA_APP_CERT` | Token generation | `live_match.py --video-h264`, `tokens.py` |
| `SPORTRADAR_API_KEY` | Live match data | `commentary_feeder.py` |

## License

MIT
