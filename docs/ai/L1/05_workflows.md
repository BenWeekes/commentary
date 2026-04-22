# L1 — Workflows

## Run Modes

All modes use the multi-session architecture: the server waits for viewers to create sessions via the HTTP API. Each viewer gets its own Agora channel, token, and language preference.

### Full demo (video + STT + events → Agora)

```bash
python3 live_match.py \
    --audio data/audio/bmg_fch_match_35_40.mp3 \
    --video-h264 go-audio-video-publisher/encoded_assets/bmg_fch_match_35_40.h264 \
    --events data/events/bmg_fch_35_40_clip.txt \
    --lang es
```

Requires: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `AGORA_APP_ID`, `AGORA_APP_CERT`

Open `viewer.html` in a browser, select language, click Start.

### Events only (simplest — no Deepgram key needed)

```bash
python3 live_match.py \
    --events data/events/bmg_fch_35_40_clip.txt \
    --video-h264 go-audio-video-publisher/encoded_assets/bmg_fch_match_35_40.h264 \
    --lang es
```

Replays pre-timed events through TTS. No STT.

### STT + Events (no video)

```bash
python3 live_match.py \
    --audio data/audio/bmg_fch_match_35_40.mp3 \
    --events data/events/bmg_fch_35_40_clip.txt \
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

## Available Synced Clips

| Clip | Match time | Video file | Audio file | Events file |
|---|---|---|---|---|
| 35–40 min | 35:00–40:00 | `bmg_fch_match_35_40.h264` | `bmg_fch_match_35_40.mp3` | `bmg_fch_35_40_clip.txt` |

The events file offsets are relative to clip start (0 = match minute 35:00).

## Multi-Session Viewer

```bash
# Start server
python3 live_match.py \
    --audio data/audio/bmg_fch_match_35_40.mp3 \
    --video-h264 go-audio-video-publisher/encoded_assets/bmg_fch_match_35_40.h264 \
    --events data/events/bmg_fch_35_40_clip.txt \
    --lang es

# Open viewer (no URL params needed except optional lang)
open "viewer.html?lang=es"
# Or with custom control server
open "viewer.html?ctl=http://localhost:8090&lang=fr"
```

Each viewer tab creates its own session. Multiple viewers can run simultaneously with different languages.

## Adding a New Language

1. Add language code and name to `LANG_NAMES` dict in `live_match.py`
2. Optionally add an ElevenLabs voice ID to `LANG_VOICES` dict
3. Add an `<option>` element in `viewer.html` language dropdown
4. No translation prompt changes needed — GPT-4o-mini handles any language

## Generating an Agora Token

Tokens are generated automatically by the multi-session server. For manual generation:

```python
from tokens import AccessToken, ServiceRtc

token = AccessToken("APP_ID", "APP_CERT", expire=3600)
rtc = ServiceRtc("channel-name", 101)
rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, 3600)
token.add_service(rtc)
print(token.build())
```

## Switching Language at Runtime

```bash
# Via curl (session-based)
curl "http://localhost:8090/api/session/{SESSION_ID}/set-lang?lang=fr"

# Via viewer
# Select language from dropdown — sends set-lang automatically
```

Language changes take effect on the next TTS utterance (JIT translation).

## STT benchmark

```bash
python3 stt_realtime_translate.py \
    --audio data/audio/bmg_fch_match_35_40.mp3 \
    --lang es
```

Measures per-utterance latency: STT time, translation time, total pipeline latency.
