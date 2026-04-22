# L1 — Gotchas

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

## ElevenLabs WebSocket disconnects

Under load, ElevenLabs WebSocket connections can drop silently. The TTSEngine logs `[TTS #N] WARNING: No audio received` when this happens. The pipeline continues with the next utterance.

## Deepgram keyword limit

The `TERMS_LIST` contains ~80 terms for keyword boosting. Deepgram has a limit on keyterm count. If you add too many, some may be silently ignored.

## Hardcoded default App ID in viewer.html

`viewer.html` line 238 has a hardcoded default `APPID`. Override it with `?appid=YOUR_ID` in the URL.

## Latency drops

When total pipeline latency exceeds `MAX_LATENCY_S` (3.5s), the STT pipeline drops the utterance with a `[DROP]` log. This prevents audio from falling too far behind video.
