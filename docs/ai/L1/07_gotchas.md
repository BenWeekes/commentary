# L1 — Gotchas

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

**Fix**: Always run with proper signal handling, or manually kill with `pkill -f send_h264_pcm_uid73`.

## go.mod replace directive

`go-audio-video-publisher/go.mod` line 10 has a `replace` directive pointing to a local path:

```
replace github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2 => /Users/benweekes/work/codex/...
```

**Fix**: Update this path to point to your local copy of the Agora Go Server SDK.

## DYLD_LIBRARY_PATH for Agora SDK

`live_match.py:start_publisher()` (line 637) sets `DYLD_LIBRARY_PATH` to a hardcoded relative path (`../codex/server-custom-llm/go-audio-subscriber/sdk/agora_sdk_mac`). This path won't exist in a fresh clone.

**Fix**: Set `DYLD_LIBRARY_PATH` to your Agora SDK native library directory before running:

```bash
export DYLD_LIBRARY_PATH=/path/to/agora_sdk_mac
```

Or update the path in `start_publisher()`.

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

## Deepgram keyword limit

The `TERMS_LIST` contains ~80 terms for keyword boosting. Deepgram has a limit on keyterm count. If you add too many, some may be silently ignored.

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

## WAV header size varies

`convert_to_pcm()` produces WAV files with variable-size headers (typically 78 bytes, not the assumed 44). Always use `wave.open()` to read PCM data, never hardcode header offsets.
