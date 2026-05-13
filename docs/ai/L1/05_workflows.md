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
- `recordings.json` — per-language cloud recording session metadata, upload status, and expected S3 HLS URLs when cloud recording is enabled
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
- `source.type = srt_direct` for a direct SRT pull that bypasses the media-gateway path, exposes local PCM/H.264 to the translated path, and separately publishes one buffered original Agora channel for viewing

For `source.type = srt`:

- the SRT source is republished into `source.ingest_channel`
- one combined program feed is published on `source.publish_uid`
- STT reads that combined program audio
- translated output channels carry delayed video + translated TTS only
- there is no separate source-atmosphere bed in SRT mode

For `source.type = srt_direct`:

- the SRT source is pulled once
- the SRT endpoint is treated as single-caller; do not run another probe/subscriber against the same URL while the ingester is live
- the viewer-facing original channel is published into `source.original_channel` with encoded video preserved
- `source.original_buffer_seconds` adds a small source-side delay for original viewing jitter smoothing
- `source.audio_stream_index` selects the commentary/program audio stream for STT and original-channel commentary
- `source.atmosphere_audio_stream_index` optionally selects a separate atmosphere stream
- Python STT reads commentary PCM from the source process directly
- per-language `relay_publish.go` reads cleaned local H.264 from the source process directly; the ingester converts SRT H.264 to Annex B if needed, parses full access units, drops `AUD`/filler NALs, and carries SPS/PPS forward for keyframes
- per-language `relay_publish.go` reads delayed atmosphere PCM from the source process when configured, then mixes it with translated TTS
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
7. `srt_direct`: per-language `relay_publish.go` connects to the source process's local H.264 and optional atmosphere PCM sockets, and uses the full configured `video_delay`
8. Viewers connect to per-language output channels

### Standalone one-language live test

Use `test_live_pipeline.py` to prove the live media path independently of full match orchestration.

What it exercises:

1. `subscribe_audio` subscribes to the source commentary UID
2. The configured live STT provider reads live PCM from the source publisher
3. GPT translation runs with `gpt-5.4-mini` and `reasoning_effort="low"` by default in this standalone script; pass `--translation-model gpt-5.4` to match the current server live config
4. ElevenLabs TTS generates PCM for one target language; server mode keeps ElevenLabs speed at `1.0` and uses local ffmpeg `atempo` speed fitting when a generated clip must fit before the next STT item
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

### Demo artifacts through local SRT

Use this when you need to test live SRT timing with known demo media. `source.type = demo_srt_direct` starts one owned FFmpeg process for the configured demo media, publishes it as MPEG-TS over local SRT, then runs the normal `srt_direct` live path against it. Demo SRT sources default to a single pass (`demo_loop: false`) so cloud recordings do not accidentally contain repeated games.

On `status.html`, manual demo-live rows expose an STT provider selector before Start. Use `Deepgram Nova-3` and `Soniox` runs on the same local SRT demo source when comparing recognition quality; the chosen provider is sent as `stt_provider` in the start request and logged by `MatchWorker`. The current preferred eval-demo setting is Soniox with `stt_endpoint_delay_ms=1500`.

`matches_live.yaml` also includes `m05_uni_eval_demo`, which loops the reviewed Mainz vs Union evaluation section through the same live path on local SRT port `10081`. The generated source file is `clips/m05_uni_eval_25min/source.mp4` and is intentionally ignored by git as a derived media artifact. This row has one program/commentary audio track and no separate atmosphere track, so `audio_stream_index: 1` and `atmosphere_audio_stream_index: -1`. Include `en` in the language list when generating shareable eval recordings; that channel speaks the corrected English STT via TTS and is useful for ear-checking sync against Original.

Provider timing is normalized with per-provider offsets before TTS scheduling. The latency marker clip currently uses `soniox: 700ms` and `deepgram_nova3: 830ms`. These offsets compensate provider word/onset semantics only; the shared scheduler still uses the same source media clock and `video_delay` for all providers.

`matches_live.yaml` includes `latency_test`, a short demo-live row for visible/spoken timing markers. Build or rebuild the source clip with:

```bash
DURATION=300 INTERVAL=5 OUT=clips/latency_test/source.mp4 tools/build_latency_test_clip.sh
```

Start `latency_test` from `status.html`, then compare Original with `en` or another translated channel. The visible `MARK N` should align with the spoken "Mark N seconds" phrase after the configured provider offset is applied.

Speaker-specific TTS voices can be configured per match with:

```yaml
speaker_voice_ids:
  default:
    s0: ELEVENLABS_VOICE_ID_FOR_SPEAKER_0
    s1: ELEVENLABS_VOICE_ID_FOR_SPEAKER_1
```

Per-language maps such as `de: {s0: "...", s1: "..."}` override `default`. The mapping only takes effect when the selected STT provider emits speaker labels.

Start the demo-live server:

```bash
python3 -m server.main --config matches_demo_live_srt.yaml
```

Then start `bmg_fch_demo_srt` from the status page. Start owns both the local SRT looper and the match worker; Stop terminates both. Only one looper can bind the configured `demo_srt_port`, so concurrent starts on the same port are rejected. Open:

```text
http://localhost:8081/viewer_live.html?match=bmg_fch_demo_srt&lang=en
```

The demo SRT stream layout matches the live two-audio-track case: stream `0` is H.264 video, stream `1` is atmosphere AAC, and stream `2` is commentary AAC. The YAML therefore uses `atmosphere_audio_stream_index: 1` and `audio_stream_index: 2`. `tools/run_demo_srt_listener.sh` is kept as a manual probe for ffprobe/player testing, but the status-page workflow should use the owned `demo_srt_direct` source.

When `en` is configured as a language, the English output channel is not the raw source audio. It still goes through live SRT pull, H.264 cleanup, local commentary PCM to STT, deterministic roster/keyterm name correction, live `play_at` scheduling, ElevenLabs TTS, and per-language relay publishing. This verifies the live clock and translated-channel path rather than the file-backed demo scheduler.

Recent eval learnings:

- Soniox realtime `stt-rt-v4` was more accurate and faster than Deepgram Nova-3 on the Mainz/Union gold clip.
- Deepgram Nova-3 provides useful word end times for the latency marker clip, but Soniox remains the preferred eval-demo STT provider for the Mainz/Union football clip.
- `stt_endpoint_delay_ms=1500` gives more natural turns than very short endpoints while staying inside the 14s live delay for most turns.
- Long Soniox turns must still be force-emitted with `max_stt_duration`; otherwise oversized chunks can miss `play_at` before translation starts.
- Live correction should be deterministic and keyterm-driven. `GLOBAL_FOOTBALL_CORRECTIONS` handles high-confidence football phrases for both Deepgram and Soniox; roster/keyterm name correction then fixes proper names. An LLM correction pass found useful fixes but had high tail latency and could make semantic overcorrections.
- TTS tempo fitting should target the known gap before the next STT item, not raw STT provider word spans. Current bounds are `0.769x` to `1.3x`; if the next STT play time is unknown, keep the generated duration.

For a short live-clock smoke test, run `test_live_pipeline.py` with `--assert-skew-ms 50 --stop-after-utterances N` against a live/demo source. The test fails if live `intended_skew_ms` drifts beyond the threshold.

### Evaluation tooling

Use the offline/realtime eval tools when changing STT provider settings, endpointing, keyterms, or translation strategy. They do not start Agora publishers or cloud recordings; they stream a fixed WAV at realtime pace and write artifacts under `match_data/.../eval/...`.

Realtime STT provider comparison:

```bash
.venv/bin/python tools/run_live_stt_eval.py \
  --audio match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav \
  --gold match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json \
  --keyterms match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt \
  --providers flux,nova,soniox \
  --nova-configs 500:1500:8 \
  --flux-configs 0.8:2000 \
  --soniox-endpoints 1000,1500 \
  --out match_data/m05_uni_md33/eval/20260510_190915/live_stt_eval
```

Outputs include `summary.md`, `summary.json`, and per-provider `turns.json` / `score.json`. The headline metrics are WER versus the gold transcript, turn fragmentation, boundary timing, and median/p90 STT latency.

Translation strategy comparison:

```bash
.venv/bin/python tools/run_translation_eval.py \
  --audio match_data/m05_uni_md33/eval/20260510_190915/source_mono_16000.wav \
  --turns match_data/m05_uni_md33/eval/20260510_190915/live_stt_eval/soniox_rt_endpoint1500/turns.json \
  --keyterms match_data/m05_uni_md33/eval/20260510_190915/soniox_improved/improved_keyterms.txt \
  --lang es \
  --duration 300 \
  --out match_data/m05_uni_md33/eval/20260510_190915/translation_eval_es
```

Outputs include `summary.md`, `summary.json`, `aligned_translation_compare.json`, `soniox_translation_turns.json`, and `gpt_translation_turns.json`. Use it to compare Soniox streaming translation latency/wording with the current GPT full-turn translation before changing the live translation path.

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
  - SRT direct live source with `atmosphere_audio_stream_index`: delayed atmosphere + translated TTS
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
