# L1 — Gotchas

> Known pitfalls, hardcoded paths, edge cases, and their workarounds.

## Server port conflicts

The production server binds to port 8080 by default. If a stale server process is running, the new one will fail with `Address already in use`.

**Fix**: Kill stale processes before restarting:

```bash
lsof -ti:8080 | xargs kill -9
```

The dev-mode server (`live_match.py`) uses port 8090, so both can run simultaneously.

## matches.yaml path resolution

File paths in `matches.yaml` (audio, video_h264, events, atmosphere) are resolved **relative to the config file's directory**, not the working directory. If `matches.yaml` is in the repo root and references `clips/bmg_fch_demo_5min/audio.mp3`, the path resolves relative to the repo root.

**Common mistake**: Moving `matches.yaml` to a subdirectory without updating paths.

**Fix**: Use `--dry-run` to validate all paths before starting:

```bash
python3 -m server.main --config matches.yaml --dry-run
```

The same applies to generated standalone test configs such as `matches_live_test.yaml`.

## configured_languages vs languages

The production server API returns two different language fields:

- `configured_languages`: from `matches.yaml` — the languages the match is set up to support
- `languages`: runtime state — per-language pipeline status (only populated after match starts)

Before a match starts, `languages` is empty even though `configured_languages` lists all target languages. The viewer uses `configured_languages` to populate the language dropdown and `languages` to show per-language status.

## Source MP4 kickoff offset

The Sportradar BMG vs FCH MP4 (`soccer_germany_bundesliga_8321531_3064k.mp4`) has **29:58 of pre-match content** before kickoff. The second half starts at **1:34:36** file time.

| Moment | File time |
|---|---|
| Kickoff | 29:58 |
| Second half | 1:34:36 |

**Common mistake**: Extracting at `-ss 00:35:00` gives match minute ~5:00, not 35:00. You must add ~30 min to match time: match time 35:00 → file time ~01:05:00.

See `docs/ai/L1/05_workflows.md` for the full extraction formula.

## Go publisher zombie processes

`live_match.py` launches the Go publisher via `subprocess.Popen` with `preexec_fn=os.setsid` (new process group). The `kill_publisher()` function kills the entire process group with `os.killpg(SIGKILL)`. If the Python process crashes without calling cleanup, the Go publisher and its child processes (Go compiler spawns a child) remain as zombies.

The server mode (`match_worker.py`) uses the same pattern — `_kill_publisher()` sends `SIGKILL` to the process group.

**Fix**: Always run with proper signal handling, or manually kill with `pkill -f send_h264_pcm_uid73`.

## go.mod replace directive

`go-audio-video-publisher/go.mod` line 10 has a `replace` directive pointing to a local path:

```
replace github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2 => /Users/benweekes/work/codex/...
```

**Fix**: Update this path to point to your local copy of the Agora Go Server SDK.

## DYLD_LIBRARY_PATH for Agora SDK

`start_publisher()` (in both `live_match.py` and `server/match_worker.py`) uses a default `DYLD_LIBRARY_PATH` pointing to a local dev path. This default only applies if `DYLD_LIBRARY_PATH` is not already set in the environment.

**Fix**: Export your own SDK path before running — it will be used instead of the default:

```bash
export DYLD_LIBRARY_PATH=/path/to/agora_sdk_mac
```

## Encoded video assets not included

The `go-audio-video-publisher/encoded_assets/` and `clips/` directories are excluded from the repo (multi-GB). Users must generate their own H.264 files.

**Fix**: Use the ffmpeg command in the README to convert an MP4 to H.264.

## Data file paths changed

In the original sportradar repo, data files were at the root. In this repo, they're under `data/`:

| Original | New |
|---|---|
| `bmg_fch_first_5min.mp3` | `data/audio/bmg_fch_first_5min.mp3` |
| `bmg_fch_md28_full_match.txt` | `data/events/bmg_fch_md28_full_match.txt` |
| `bmg_fch_md28_all_data.json` | `data/json/bmg_fch_md28_all_data.json` |

All CLI examples in the README use the new `data/` prefix paths.

## ElevenLabs TTS returns no audio for short phrases

Very short phrases (e.g., "to Scally.") sometimes produce zero audio bytes from ElevenLabs. The `_tts` method retries once with padded text (`text + "..."`) when this happens. Logs: `[TTS #N] WARNING: No audio received (will retry)`.

## ElevenLabs WebSocket disconnects

Under load, ElevenLabs WebSocket connections can drop silently. The TTSEngine logs `[TTS #N] WARNING: No audio received` when this happens. The pipeline continues with the next utterance.

## gpt-5.4-mini blank responses

`gpt-5.4-mini` with `reasoning_effort="medium"` occasionally returns empty strings, especially for garbled or ambiguous STT input. This cascades to empty translations for all languages on that utterance.

**Workarounds**:
- Use `reasoning_effort="low"` — much lower blank rate
- Use `gpt-4o-mini` — no blank responses observed (server mode default)
- Use `max_completion_tokens` (not `max_tokens`) with gpt-5.4-mini — `max_tokens` returns HTTP 400

## gpt-5.4-mini parameter differences

`gpt-5.4-mini` uses `max_completion_tokens` instead of `max_tokens`. The `translate_text()` function in `lib/translator.py` handles this: when `reasoning_effort` is set, it uses `max_completion_tokens`; otherwise it uses `max_tokens` with `temperature`.

## Deepgram keyword limit

The `TERMS_LIST` (in `lib/corrections.py`) contains ~91 terms for keyword boosting. Deepgram has a limit on keyterm count. If you add too many, some may be silently ignored.

## Hardcoded default App ID in viewer.html

`viewer.html` has a hardcoded default `APPID`. In multi-session mode, the App ID is returned by `POST /api/session` and overrides the default.

## Latency drops

When total pipeline latency exceeds `MAX_LATENCY_S` (3.5s), the STT pipeline drops the utterance with a `[DROP]` log. This prevents audio from falling too far behind video.

## Atmosphere volume tuning

Mel-Band Roformer separated atmosphere has reasonable amplitude. The default `_atmosphere_vol` is 0.5x to sit under commentary without clipping. Increase if crowd noise is too quiet; decrease if it distorts.

## Atmosphere and original audio require restart

Both `--atmosphere` and `--audio` load PCM into memory at startup. Changes to these files or adding them after the server starts require a restart. Check for `[ATMOS] Loaded Xs` and `[ORIG] Loaded Xs` in startup logs.

## Language switch can bleed old-language audio

On language change, queued STT utterances are flushed to prevent old-language playback. SR prefetched events are also flushed and re-translated. However, an utterance already being synthesized by ElevenLabs will complete in the old language.

## Structured logs are per run, not per match id forever

Each server-mode match start creates a new directory:

```text
logs/{match_id}_{YYYYMMDD_HHMMSS}/
```

If you restart the same match multiple times, you get multiple directories. Post-match tooling must not assume a single stable `logs/{match_id}/` path.

## `--test-id` is not an Agora token

In `test_live_pipeline.py`, `--test-id` is only a short naming input used to derive:

- `match_id = livepipe_<test-id>`
- `source_channel = livepipe_<test-id>_src`
- `output_channel = livepipe_<test-id>-<lang>`

It is **not** an Agora RTC token and is not sent to Agora as a credential.

## JSONL logs are best-effort, not transactional

`stt.jsonl` and `{lang}.jsonl` are line-buffered and flushed after each write, so mid-match crashes usually preserve recent lines. Telemetry callbacks from multiple threads (pipe_writer, SR scheduler) are serialized by `_telemetry_lock` in MatchWorker to prevent interleaved writes and racy counter increments. They are still ordinary local files:

- a hard kill can still lose the last in-flight line
- header/write ordering is only guaranteed within one file handle
- playback telemetry is richer than stdout logs, but not a replacement for full trace infrastructure

## WAV header size varies

`convert_to_pcm()` produces WAV files with variable-size headers (typically 78 bytes, not the assumed 44). `pcm_chunks_realtime()` uses `wave.open()` to read PCM data correctly.

## video_start is estimated before publisher confirms

STT starts processing audio during the video delay, before the Go publisher confirms video has started. The pipeline sets a temporary `video_start = time.time() + video_delay` (the `target_start`) and schedules early STT utterances against it. Once the publisher reports "video delay complete", `pipe.video_start` and `_video_start_ref[0]` are updated to the actual value. Subsequent STT utterances use the corrected timestamps. Utterances scheduled before the correction may have slight drift (typically <50ms).

In server mode, each language publisher has its own `video_start`. The MatchWorker warns if any publisher's actual `video_start` drifts more than 500ms from `target_start`.

## Related Deep Dives

- [TTSEngine Internals](L2/tts_engine.md) — buffer underrun and interrupt edge cases
- [STT Pipeline](L2/stt_pipeline.md) — forced split and latency drop details
