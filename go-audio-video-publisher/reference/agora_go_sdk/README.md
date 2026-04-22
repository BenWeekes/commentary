# Agora Go SDK References

These are local reference copies of official Agora Go Server SDK examples.

Why they are here:
- to keep the working test variants inside this repo instead of `/tmp`
- to preserve the exact int-UID experiments that were useful during debugging
- to avoid modifying the checked-out upstream SDK example files directly

What is here:
- `send_h264_uid73.go`
  - based on the official `send_h264` example
  - changed to use `BuildTokenWithUid(..., 73, ...)`
  - connects with `"73"` so the SDK uses int-UID flow
  - useful for video-only encoded H.264 testing from this repo
- `send_h264_pcm_uid73.go`
  - based on the official `send_h264` example plus a repo-local PCM audio loop
  - publishes encoded H.264 video and raw PCM audio on the same int UID
  - this is the current best-known combined sender path for content derived from the football MP4
- `send_encoded_audio_uid74.go`
  - based on the official `send_encoded_audio` example
  - changed to use `BuildTokenWithUid(..., 74, ...)`
  - removes the local playback observer that was crashing
  - still not considered stable; encoded audio was failing in this environment

Notes:
- these files are references, not the primary app entrypoint
- the main standalone publisher remains [main.go](/Users/benweekes/work/codex/go-audio-video-publisher/main.go)
- the current end-to-end instructions are in [README.md](/Users/benweekes/work/codex/go-audio-video-publisher/README.md)
- keep credentials out of these files; supply them by env vars at runtime
