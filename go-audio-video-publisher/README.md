# go-audio-video-publisher

Standalone test app for publishing media derived from an MP4 file into an Agora RTC channel.

This lives outside `agent-uikit` on purpose. It started from the local Agora Go Server SDK examples, but the currently verified path is a repo-local reference sender that publishes:

- encoded H.264 elementary-stream video
- raw 16 kHz mono PCM audio
- on the same int UID

## Verified Working Path

The path that is currently verified end to end is:

1. start from an H.264/AAC `.mp4`
2. convert it to:
   - `.h264` elementary-stream video
   - `.pcm` 16 kHz mono audio
3. run [reference/agora_go_sdk/send_h264_pcm_uid73.go](/Users/benweekes/work/codex/go-audio-video-publisher/reference/agora_go_sdk/send_h264_pcm_uid73.go)

That is the path that successfully published football video and football audio together on the same int UID in the target client.

## Requirements

- FFmpeg development libraries are installed locally
- FFmpeg CLI tools are available
- the Agora native SDK assets are downloaded under `../server-custom-llm/go-audio-subscriber/sdk`
- on macOS, set `DYLD_LIBRARY_PATH` to `../server-custom-llm/go-audio-subscriber/sdk/agora_sdk_mac`

## Setup

1. Ensure the native Agora SDK assets are present:

```bash
cd /Users/benweekes/work/codex/server-custom-llm/go-audio-subscriber/sdk
make deps
```

2. Ensure FFmpeg and pkg-config are installed and visible in the shell.

3. Build or run from this repo:

```bash
cd /Users/benweekes/work/codex/go-audio-video-publisher
go build .
```

4. Create local working directories for generated media:

```bash
mkdir -p clips raw_assets encoded_assets
```

## Convert From The Original MP4

Example source:

```bash
SOURCE_MP4=/absolute/path/to/input.mp4
```

Optional: cut a 5-minute clip first:

```bash
ffmpeg -hide_banner -y \
  -ss 00:35:00 \
  -t 300 \
  -i "$SOURCE_MP4" \
  -c copy \
  clips/clip_35m_40m.mp4
```

For repeat use, treat the extracted clip as a variable:

```bash
CLIP_MP4=clips/clip_35m_40m.mp4
```

Create the audio asset used by the working sender:

```bash
ffmpeg -hide_banner -y \
  -i "$CLIP_MP4" \
  -vn \
  -ac 1 \
  -ar 16000 \
  -f s16le \
  raw_assets/clip_16kmono.pcm
```

Create the preferred encoded H.264 video asset used by the working sender:

```bash
ffmpeg -hide_banner -y \
  -i "$CLIP_MP4" \
  -an \
  -vf scale=1280:720,fps=25 \
  -pix_fmt yuv420p \
  -c:v libx264 \
  -profile:v high \
  -level 3.1 \
  -preset veryfast \
  -x264-params keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1 \
  -b:v 2800k \
  -maxrate 3200k \
  -bufsize 6400k \
  -f h264 \
  encoded_assets/clip_720p25_high.h264
```

That recipe is intended to match the stream shape that worked in testing:

- H.264 elementary stream, not MP4
- 1280x720
- 25 fps
- yuv420p
- no B-frames
- repeated SPS/PPS headers

If you need a lower-bitrate compatibility fallback, this older 352x288 recipe also worked:

```bash
ffmpeg -hide_banner -y \
  -i "$CLIP_MP4" \
  -an \
  -vf scale=352:288,fps=25 \
  -pix_fmt yuv420p \
  -c:v libx264 \
  -profile:v high \
  -level 1.3 \
  -preset veryfast \
  -x264-params keyint=25:min-keyint=25:scenecut=0:ref=1:bframes=0:repeat-headers=1 \
  -f h264 \
  encoded_assets/clip_352x288_25.h264
```

## Publish The Converted Assets

Use an int UID. The current reference sender is hard-wired for int-UID token flow and was validated with UID `73`.

```bash
export AGORA_APP_CERTIFICATE=your-app-certificate
export DYLD_LIBRARY_PATH=/Users/benweekes/work/codex/server-custom-llm/go-audio-subscriber/sdk/agora_sdk_mac

go run ./reference/agora_go_sdk/send_h264_pcm_uid73.go \
  your-app-id \
  your-channel \
  /absolute/path/to/encoded_assets/clip_720p25_high.h264 \
  /absolute/path/to/raw_assets/clip_16kmono.pcm
```

The sender will:

- connect as UID `73`
- publish video first
- start audio after a short warmup
- loop both assets

To publish a different extracted clip later, just change:

- `CLIP_MP4`
- the output asset filenames under `raw_assets/` and `encoded_assets/`
- the two file arguments passed to `send_h264_pcm_uid73.go`

To use a different int UID in the reference sender, update both occurrences of `73` in [reference/agora_go_sdk/send_h264_pcm_uid73.go](/Users/benweekes/work/codex/go-audio-video-publisher/reference/agora_go_sdk/send_h264_pcm_uid73.go):

- `userID := "73"`
- `BuildTokenWithUid(..., 73, ...)`

## Main App vs Reference Sender

- [main.go](/Users/benweekes/work/codex/go-audio-video-publisher/main.go) contains the standalone app and several experimental paths
- [reference/agora_go_sdk/send_h264_uid73.go](/Users/benweekes/work/codex/go-audio-video-publisher/reference/agora_go_sdk/send_h264_uid73.go) is the repo-local encoded video reference sender
- [reference/agora_go_sdk/send_h264_pcm_uid73.go](/Users/benweekes/work/codex/go-audio-video-publisher/reference/agora_go_sdk/send_h264_pcm_uid73.go) is the current best-known combined sender for encoded football video plus PCM football audio on one int UID

If you want the path that is actually known to work today, use the reference sender, not the generic `--input` MP4 path.

## Token Generation

The working reference senders use Agora's official DynamicKey Go package:

- package: `github.com/AgoraIO/Tools/DynamicKey/AgoraDynamicKey/go/src/rtctokenbuilder2`
- function: `BuildTokenWithUid`

That produces a token for a true int UID, not a user-account string token.

## Notes

- The direct MP4 publish path is still useful for experiments, but it is not the most reliable route for this content.
- `PushVideoEncodedData ret=1` can still appear even on streams that render correctly in the target client, so treat visibility in the client as the real success signal.
- The working football path is currently:
  - encoded video from `.h264`
  - PCM audio from `.pcm`
  - same int UID
- The preferred quality path is now 16:9 `1280x720 @ 25fps` H.264 at roughly `2.8 Mbps`.
- Generated media under `clips/`, `raw_assets/`, and `encoded_assets/` is intentionally ignored by git.
- Audio must be framed in 10 ms PCM chunks for `PushAudioPcmData`.
- Agora SDK logs from the standalone app are written under `./agora_rtc_log/`.
