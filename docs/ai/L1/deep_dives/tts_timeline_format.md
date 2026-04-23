# TTS Timeline Analysis Format

Use this format when analysing TTS playback logs. It correlates what the viewer hears with what was expected and when playout completes.

## Log Extraction Command

```bash
grep -E "\[TTS #|DROP|Queue empty|SR " /tmp/commentary_server.log \
  | grep -v "First audio" | grep "V+"
```

## Timeline Table Format

```
| # | V+ Start | Expected | Delta | V+ End | Words | Content | Source |
|---|---|---|---|---|---|---|---|
| 1 | 00:04.8 | STT @0.0s | — | 00:06.2 | 4 | "Contra los Billy Coats." | STT |
| 4 | 00:12.01 | SR @9s (+3=12) | +10ms | 00:13.0 | 3 | "Ramaj a Busch." | SR EVT |
| **13** | **00:36.02** | **SR @33s (+3=36)** | **+20ms** | **00:42.1** | **23** | **"¡GOL! ¡Mohya dispara..."** | **SR INT** |
```

### Column definitions

| Column | Description |
|---|---|
| # | Utterance number from `[TTS #N]` log lines |
| V+ Start | Video-relative time when TTS playback begins (from `Playback started` or `Buffered...starting playback`) |
| Expected | When this utterance *should* have played. For SR events: `SR @Ns (+delay=target)`. For STT: `STT @Ns` (audio_start time from Deepgram) |
| Delta | Difference between V+ Start and Expected. Target: **±100ms** for SR events. `—` for STT (best-effort). Computed as `V+ Start - Expected` |
| V+ End | Video-relative time when TTS playback ends (from `[TTS #N] Done` log line) |
| Words | Word count from `(Nw, ...)` in the Starting log line |
| Content | The translated text spoken. Truncated in logs at 50 chars |
| Source | `STT` = live Deepgram transcription, `SR EVT` = Sportradar APPEND event, `SR INT` = Sportradar INTERRUPT event |

### How to read V+ Start and V+ End from logs

- **V+ Start**: Look for `[TTS #N] Buffered ... starting playback` or `holding Xs for sync` followed by `[PIPE] Playback started`. The V+ time on the `Playback started` line is V+ Start.
- **V+ End**: Look for `[TTS #N] Done` — the V+ time on that line is V+ End.

### Key metrics to check

- **Delta (SR events)**: Must be within **±100ms**. The `holding Xs for sync` log shows how long the TTS worker held before signaling playback. The tight-spin hold targets ±1ms; total chain jitter (spin + event wake + pipe write) should be <5ms
- **Goal latency**: V+ Start of GOAL TTS minus expected (offset + video_delay). Should be <100ms
- **TTFB**: Time from `Starting` to `First audio chunk received` (ElevenLabs response time)
- **Idle gaps**: `Queue empty — idle` lines show silence periods
- **Drops**: `[DROP Xs]` lines show STT utterances dropped for exceeding latency budget
- **Late events**: `(play in -Xs)` means the SR event played X seconds after its scheduled time — if TTS fetch took longer than the hold window
- **Interrupts**: `Interrupted after Xs` confirms INTERRUPT events cleared the queue

### Debug video

Use `video_debug.h264` which has `V HH:MM:SS.ms` burnt into the top-left corner. This directly corresponds to the `V+` prefix in log timestamps.

Generate debug video from any clip:
```bash
ffmpeg -hide_banner -y -f h264 -framerate 25 -i video.h264 \
  -vf "drawtext=text='V %{pts\:hms}':fontsize=28:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10" \
  -pix_fmt yuv420p -c:v libx264 -profile:v high -level 3.1 -preset veryfast \
  -x264-params "keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1" \
  -b:v 2800k -maxrate 3200k -bufsize 6400k \
  -f h264 video_debug.h264
```
