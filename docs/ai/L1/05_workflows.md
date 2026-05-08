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
match_data/{match_id}/runs/{YYYYMMDD_HHMMSS}/
```

Useful files:

- `stt.jsonl` — shared STT utterance log with header, keyterms, roster, and per-utterance timestamps
- `{lang}.jsonl` — per-language translation/TTS/playback outcomes (`played`, `dropped`, `interrupted`, `suppressed`)
- `match_data/{match_id}/latest_run.txt` — newest run pointer

Quick inspection:

```bash
cat match_data/bmg_fch_demo/latest_run.txt
ls -1 match_data/bmg_fch_demo/runs/
tail -20 match_data/bmg_fch_demo/runs/20260508_123456/stt.jsonl
tail -20 match_data/bmg_fch_demo/runs/20260508_123456/es.jsonl
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

### SportPortal home

`http://localhost:8080/status.html` — authenticated ops dashboard with match cards, scheduler state, countdown, start/stop (demo), and refresh (live). `/` and `/control.html` redirect here. `control.html` has been retired.

## Live Match Workflow

### Live match config

Live matches are configured in `matches_live.yaml`. Live entries support a nested `source:` block:

- `source.type = agora` for an existing Agora source channel with explicit source UIDs
- `source.type = srt` for a direct SRT pull that is republished into an internal Agora source channel
- `source.type = srt_direct` for a direct SRT pull that exposes local PCM/H.264 to the translated path and separately publishes one buffered original Agora channel for viewing

For `source.type = srt`:

- the SRT source is republished into `source.ingest_channel`
- one combined program feed is published on `source.publish_uid`
- STT reads that combined program audio
- translated output channels carry delayed video + translated TTS only
- there is no separate source-atmosphere bed in SRT mode

For `source.type = srt_direct`:

- the SRT source is pulled once
- the viewer-facing original channel is published into `source.original_channel` with encoded video preserved
- `source.original_buffer_seconds` adds a small source-side delay for original viewing jitter smoothing
- Python STT reads local PCM from the source process directly
- per-language `relay_publish.go` reads cleaned local H.264 from the source process directly
- translated relay delay stays equal to `video_delay`; the original-channel buffer affects viewer-original playback only

```bash
# Start server with live config
python3 -m server.main --config matches_live.yaml
```

Legacy flat live fields (`source_channel`, `video_uid`, `atmosphere_uid`, `commentary_uid`) still load as `source.type = agora` during migration.

### Pre-match

1. Configure match in `matches_live.yaml` with source channel and language list
2. Start the server: `python3 -m server.main --config matches_live.yaml`
3. For auto-managed live matches, set `prestart_seconds` if you want warm-up coverage before kickoff
4. Match stays idle until manual start or until the scheduler reaches the prestart window

### Live match start

1. Resolve the configured live source:
   - `agora`: use the configured source channel/UIds directly
   - `srt`: start one SRT ingest process and wait for `source publishing started`
   - `srt_direct`: start one single-pull source process, wait for `local sources ready`, then for `source publishing started`
2. Start match via API: `curl -X POST http://localhost:8080/api/matches/{id}/start`
3. `agora` / `srt`: `subscribe_audio.go` subscribes to the resolved source channel and commentary/program UID, writing PCM to stdout
4. `agora` / `srt`: Python STT reads from `subscribe_audio` stdout via `pcm_stream_from_pipe()`
5. `srt_direct`: Python STT connects to the source process's local PCM socket
6. `agora` / `srt`: per-language `relay_publish.go` subscribes to delayed source video and optional atmosphere, reads TTS from stdin, publishes to output channels
7. `srt_direct`: per-language `relay_publish.go` connects to the source process's local H.264 socket and uses the full configured `video_delay`
8. Viewers connect to per-language output channels

### Standalone one-language live test

Use `test_live_pipeline.py` to prove the live media path independently of full match orchestration.

What it exercises:

1. `subscribe_audio` subscribes to the source commentary UID
2. Deepgram STT reads live PCM from the subscriber stdout
3. GPT translation runs with `gpt-5.4-mini` and `reasoning_effort="low"`
4. ElevenLabs TTS generates PCM for one target language
5. `relay_publish` republishes delayed video + delayed atmosphere + translated TTS into a separate output channel

Real-source example:

```bash
python3 test_live_pipeline.py \
    --source-channel bvb_sge_md33 \
    --output-channel bvb_sge_md33-es-test \
    --lang es \
    --video-delay 10 \
    --match-id bvb_sge_md33 \
    --sport-event-id sr:sport_event:61514184
```

This test is STT-only. It does **not** inject SR events or use the SR prefetcher.

### Viewer-compatible standalone test

Use `--test-id` when you want a clean source/output pair plus a `match_id` compatible with the production viewer:

```bash
python3 test_live_pipeline.py \
    --lang es \
    --test-id e2e01 \
    --video-delay 10 \
    --sport-event-id sr:sport_event:61514184 \
    --write-test-config matches_live_test.yaml \
    --prepare-only
```

Derived names:

- `match_id = livepipe_e2e01`
- `source_channel = livepipe_e2e01_src`
- `output_channel = livepipe_e2e01-es`

Then:

```bash
python3 -m server.main --config matches_live_test.yaml
```

and open:

```text
http://localhost:8080/viewer_live.html?match=livepipe_e2e01&lang=es
```

### Fully standalone browser watch

`test_live_pipeline.py` also prints a standalone watch URL for `viewer_test.html`. This is a local `file://...` URL with:

- `appid`
- `channel`
- `token`
- `uid`

in the query string.

Open the printed URL directly in a browser to watch the test channel without starting the production server or using `/api/matches/{id}/channels`.

### Output channel content

Each per-language output channel contains:
- Delayed video (from source UID 73, held for `video_delay` seconds)
- Mixed audio:
  - Agora live source: delayed atmosphere + translated TTS
  - SRT live source: translated TTS only
- No original commentary (UID 75 is excluded from output)

### Notable limitations in live mode

- No SR gap-fill events — STT-only. Live mode currently refreshes Sportradar fixture metadata (kickoff, roster, keyterms) but does not inject real-time SR commentary into playback.
- No atmosphere toggle — atmosphere comes from source UID 74 via relay_publish, not from a local file

### Original audio channel

- **Demo mode**: `_start_original_pipeline()` loads the audio file as PCM and starts a Go publisher with `video_delay=0` on a dedicated channel `{match_id}-original`. The original plays ahead of translated channels with A/V in sync. This runs in a background thread to avoid blocking translated pipeline startup.
- **Live mode**: The `/api/matches/{id}/channels` endpoint returns the resolved live source/original channel as the "original" entry. For `source.type = srt`, that is the internal ingest channel. For `source.type = srt_direct`, that is `source.original_channel`.

The viewer shows "Original (EN)" first in the language dropdown. Default selection skips original and picks the first translated language.

### Scheduler-managed live matches

`server/scheduler.py` now manages `auto_manage: true` live matches:

- refreshes Sportradar fixture metadata on a cadence based on time-to-kickoff
- tracks kickoff countdown and scheduler state
- auto-starts a live match when it enters `prestart_seconds` before kickoff (default `30`, often `900` for warm-up coverage)
- leaves demo matches and `auto_manage: false` matches under manual control

This scheduler refresh path updates per-match disk data only. If you use the `Refresh Data` button while a match is already running, the refresh is blocked and applies on the next start.

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
    --lang es --video-delay 10
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
    --lang es --video-delay 10
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
    --lang es --video-delay 10

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
