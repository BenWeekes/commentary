# L1 — Workflows

> Server mode operations, dev mode usage, clip extraction recipes, and common operational tasks.

## Server Mode (Current)

The production server manages multiple matches with per-language Agora channels.

### Start the server

```bash
python3 -m server.main --config matches.yaml
```

### Validate config first

```bash
python3 -m server.main --config matches.yaml --dry-run
```

Checks that all API keys are set, all file paths exist, and each match has at least one language.

### Start a demo match

Matches are idle on server start. Start via API:

```bash
curl -X POST http://localhost:8080/api/matches/bmg_fch_demo/start
```

This launches one STT pipeline + N Go publishers (one per language). Each language gets its own Agora channel (`bmg_fch_demo-es`, `bmg_fch_demo-pt`, etc.).

### Monitor status

- **Status page**: `http://localhost:8080/status.html` — shows all matches, per-language state, STT count, telemetry
- **API**: `GET http://localhost:8080/api/matches` — JSON status for all matches
- **Single match**: `GET http://localhost:8080/api/matches/{id}/status`
- **Transcript**: `GET http://localhost:8080/api/matches/{id}/transcript` — recent English STT text

### Inspect structured logs

Each started match creates a log directory:

```text
logs/{match_id}_{YYYYMMDD_HHMMSS}/
```

Useful files:

- `stt.jsonl` — shared STT utterance log with header, keyterms, roster, and per-utterance timestamps
- `{lang}.jsonl` — per-language translation/TTS/playback outcomes (`played`, `dropped`, `interrupted`, `suppressed`)

Quick inspection:

```bash
ls -1 logs/
tail -20 logs/bmg_fch_demo_20260508_123456/stt.jsonl
tail -20 logs/bmg_fch_demo_20260508_123456/es.jsonl
```

### View a match

Open the production viewer:

```
http://localhost:8080/viewer_live.html?match=bmg_fch_demo&lang=es
```

The viewer requests a fresh token on each language switch (on-demand, not prefetched). Each language connects to a separate Agora channel.

### Stop a match

```bash
curl -X POST http://localhost:8080/api/matches/bmg_fch_demo/stop
```

Kills all Go publishers, stops TTS engines, and cleans up the match worker.

### Control page

`http://localhost:8080/control.html` — admin interface with start/stop buttons for each match.

## Live Match Workflow (Planned)

**Planned** — not yet implemented. Describes the intended operational workflow for live Bundesliga matches.

### Pre-match

1. Configure match in `matches.yaml` with source channel and language list
2. Start the server: `python3 -m server.main --config matches.yaml`
3. Match stays idle until kicked off

### Live match start

1. Broadcaster publishes to source Agora channel (UID 73 video, UID 74 atmosphere, UID 75 commentary)
2. Start match via API (future: auto-start via SR Schedule Monitor)
3. `subscribe_audio.go` subscribes to source channel, writes UID 75 PCM to stdout
4. Python STT reads from `subscribe_audio` stdout instead of from a file
5. `relay_publish.go` subscribes to UIDs 73 + 74, delay-buffers, publishes to output channels
6. Viewers connect to per-language output channels

### Output channel content

Each per-language output channel contains:
- Delayed video (from source UID 73, held for `video_delay` seconds)
- Mixed audio: delayed atmosphere (from source UID 74) + translated TTS
- No original commentary (UID 75 is excluded from output)

### SR Schedule Monitor (Deferred)

**Deferred** — auto-start/stop live matches based on Sportradar schedule. Would poll the schedule API and manage match lifecycles automatically.

## Dev Mode (live_match.py)

Dev mode runs a single match with per-viewer sessions. Still works and is useful for development and testing.

All modes use the multi-session architecture: the server waits for viewers to create sessions via the HTTP API. Each viewer gets its own Agora channel, token, and language preference.

### Full demo (recommended)

```bash
python3 live_match.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --video-h264 clips/bmg_fch_demo_5min/video.h264 \
    --events clips/bmg_fch_demo_5min/events.txt \
    --atmosphere clips/bmg_fch_demo_5min/atmosphere.wav \
    --lang es --video-delay 7
```

Requires: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `AGORA_APP_ID`, `AGORA_APP_CERT`

Open `http://localhost:8090/viewer.html` in a browser, select language, click Start. Viewer controls:
- **Atmos** toggle: crowd noise under commentary (Mel-Band Roformer separated)
- **Original** toggle: play source English commentary synced to video (disables lang + atmos)
- **Language** select: switch translation language on the fly

The `clips/bmg_fch_demo_5min/` directory contains all assets needed for the demo: audio, video, events, and atmosphere.

### Without atmosphere

```bash
python3 live_match.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --video-h264 clips/bmg_fch_demo_5min/video.h264 \
    --events clips/bmg_fch_demo_5min/events.txt \
    --lang es --video-delay 7
```

Same demo clip but no atmosphere mixing. The Atmos toggle will have no effect.

### Events only (simplest — no Deepgram key needed)

```bash
python3 live_match.py \
    --events clips/bmg_fch_demo_5min/events.txt \
    --video-h264 clips/bmg_fch_demo_5min/video.h264 \
    --lang es
```

Replays pre-timed events through TTS. No STT.

### STT + Events (no video)

```bash
python3 live_match.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --events clips/bmg_fch_demo_5min/events.txt \
    --lang es
```

No Agora video publishing — TTS audio goes to /dev/null. Useful for testing STT + translation pipeline.

## Extracting Clips from Source MP4

The original Sportradar match MP4s have pre-match content before kickoff. All match-time references must account for this offset.

**Source**: `/Users/benweekes/Downloads/German_Bundesliga_eng_commentary/MD28/`

| File | Content |
|---|---|
| `soccer_germany_bundesliga_8321531_3064k.mp4` | Full broadcast (2h28m), BMG vs FCH |
| `bmg_fch_commentary_from_kickoff.mp3` | English commentary from kickoff (1h58m) |

### Key Timestamps in Source MP4

| Moment | File time | Notes |
|---|---|---|
| **Kickoff (1st half)** | **29:58** | Whistle blown, match clock 0:00 |
| **Half-time** | ~1:19:58 | Approx 45+5 min after kickoff |
| **Second half start** | **1:34:36** | Whistle for 2nd half |
| **Full time** | ~2:28:00 | End of broadcast |

### Offset Formula

To extract match time `MM:SS`, calculate file time as:

- **First half**: file time = `29:58 + MM:SS` (round to `30:00 + MM:SS` for simplicity)
- **Second half**: file time = `1:34:36 + (MM:SS - 45:00)`

For most practical purposes, the **29:58 → 30:00 approximation** works within ffmpeg's keyframe tolerance.

```bash
# Match time 35:00–40:00 ≈ file time 01:04:58 (use 01:05:00)
SOURCE_MP4="/Users/benweekes/Downloads/German_Bundesliga_eng_commentary/MD28/soccer_germany_bundesliga_8321531_3064k.mp4"

# Video (H.264)
ffmpeg -hide_banner -y -ss 01:05:00 -t 300 -i "$SOURCE_MP4" -an \
    -vf "scale=1280:720,fps=25" -pix_fmt yuv420p \
    -c:v libx264 -profile:v high -level 3.1 -preset veryfast \
    -x264-params "keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1" \
    -b:v 2800k -maxrate 3200k -bufsize 6400k \
    -f h264 go-audio-video-publisher/encoded_assets/bmg_fch_match_35_40.h264

# Audio (MP3, 16kHz mono)
ffmpeg -hide_banner -y -ss 01:05:00 -t 300 -i "$SOURCE_MP4" \
    -vn -ar 16000 -ac 1 data/audio/bmg_fch_match_35_40.mp3
```

**Common mistake**: Using `-ss 00:35:00` gives you match minute ~5:00, not 35:00 (because kickoff is at 29:58 in the file).

### Second Half Example

```bash
# Match time 50:00–55:00 (second half)
# File time = 1:34:36 + (50:00 - 45:00) = 1:34:36 + 5:00 = 1:39:36
SOURCE_MP4="/Users/benweekes/Downloads/German_Bundesliga_eng_commentary/MD28/soccer_germany_bundesliga_8321531_3064k.mp4"

ffmpeg -hide_banner -y -ss 01:39:36 -t 300 -i "$SOURCE_MP4" -an \
    -vf "scale=1280:720,fps=25" -pix_fmt yuv420p \
    -c:v libx264 -profile:v high -level 3.1 -preset veryfast \
    -x264-params "keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1" \
    -b:v 2800k -maxrate 3200k -bufsize 6400k \
    -f h264 go-audio-video-publisher/encoded_assets/bmg_fch_match_50_55.h264
```

## Available Clips

| Clip | Duration | Directory / Files | Atmosphere | Notes |
|---|---|---|---|---|
| **Demo 5min** (recommended) | 5:00 | `clips/bmg_fch_demo_5min/` — `audio.mp3`, `video.h264`, `events.txt`, `atmosphere.wav` | Yes | Best demo experience — all assets bundled |
| 35–40 min | 5:00 | `data/audio/bmg_fch_match_35_40.mp3` + `encoded_assets/bmg_fch_match_35_40.h264` + `data/events/bmg_fch_35_40_clip.txt` | No | Mid-match, can have sparse commentary |
| First 5 min | 5:00 | `data/audio/bmg_fch_first_5min.mp3` | No | Pre-match / early match, audio only |

Events file offsets are relative to clip start (0 = start of clip).

## Multi-Session Viewer (Dev Mode)

```bash
# Start server (using recommended demo clip)
python3 live_match.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --video-h264 clips/bmg_fch_demo_5min/video.h264 \
    --events clips/bmg_fch_demo_5min/events.txt \
    --atmosphere clips/bmg_fch_demo_5min/atmosphere.wav \
    --lang es --video-delay 7

# Open viewer (no URL params needed except optional lang)
open "http://localhost:8090/viewer.html?lang=es"
# Or with custom control server
open "http://localhost:8090/viewer.html?ctl=http://localhost:8090&lang=fr"
```

Each viewer tab creates its own session. Multiple viewers can run simultaneously with different languages.

## Adding a New Language

1. Add language code and name to `LANG_NAMES` dict in `lib/translator.py`
2. Optionally add an ElevenLabs voice ID to `LANG_VOICES` dict in `lib/translator.py`
3. Add an `<option>` element in `viewer.html` language dropdown (dev mode)
4. Add the language code to `languages` list in `matches.yaml` (server mode)
5. No translation prompt changes needed — GPT handles any language

## Generating an Agora Token

Tokens are generated automatically by both the dev-mode multi-session server and the production server. For manual generation:

```python
from tokens import AccessToken, ServiceRtc

token = AccessToken("APP_ID", "APP_CERT", expire=86400)
rtc = ServiceRtc("channel-name", 101)
rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, 86400)
token.add_service(rtc)
print(token.build())
```

## Switching Language at Runtime

### Dev mode

```bash
# Via curl (session-based)
curl "http://localhost:8090/api/session/{SESSION_ID}/set-lang?lang=fr"

# Via viewer
# Select language from dropdown — sends set-lang automatically
```

Language changes take effect on the next TTS utterance (JIT translation).

### Server mode

In server mode, each language has its own Agora channel. Viewers switch languages by leaving one channel and joining another. The production viewer (`viewer_live.html`) handles this automatically via the language dropdown.

## Generating a Demo Transcript

Produces a timestamped multilingual transcript of the 5-min demo clip for quality review.

### Step 1: STT only (streams audio through Deepgram, ~5 min real-time)

```bash
python3 generate_demo_transcript.py --stt-only
```

Outputs: `demo_stt_cache.json` (cached results), `demo_transcript_en.txt` (English STT + SR merged by time).

### Step 2: Translate from cache (uses GPT, ~5 min with parallelism)

```bash
python3 generate_demo_transcript.py --translate
```

Fetches player roster from Sportradar, translates 100 entries × 5 languages (de, es, fr, pt, tr) with 5-way parallelism. Outputs: `demo_transcript.txt` (600 lines, 6 languages).

### Full pipeline (both steps)

```bash
python3 generate_demo_transcript.py
```

### Output format

```
time : lang : SR/STT : text
```

Ordered by time, then language (en first, then de, es, fr, pt, tr alphabetically).

### LLM comparison

`demo_llm_comparison.txt` contains side-by-side output from gpt-4o-mini (llm1) vs gpt-5.4-mini low (llm2) for all 54 STT utterances × 5 languages with per-call latency.

## STT benchmark

```bash
python3 stt_realtime_translate.py \
    --audio clips/bmg_fch_demo_5min/audio.mp3 \
    --lang es
```

Measures per-utterance latency: STT time, translation time, total pipeline latency.

## Related Deep Dives

- [STT Pipeline](L2/stt_pipeline.md) — latency budget breakdown and forced split logic
- [TTS Timeline Format](L2/tts_timeline_format.md) — log format for analysing playback timing
