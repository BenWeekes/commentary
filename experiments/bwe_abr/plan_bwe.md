# Plan — Agora Linux SDK BWE Adaptive-Bitrate Publisher Experiment

## Goal

Build a standalone C++ tool that loops the football demo content into an Agora channel via the Linux Server SDK, and dynamically adapts the published video bitrate across **6 bitrate levels** in response to per-receiver downlink BWE feedback (`onDownlinkNetworkInfoUpdated`).

This is an experiment, separate from the commentary pipeline. No integration with the Python/Go production stack — pure C++ against the same Agora SDK (4.4.32.156) that the commentary stack uses.

## Scope

- Single C++ publisher process.
- One Agora channel, one publisher UID.
- 6 pre-encoded H.264 variants of the source clip, GOP-aligned.
- ABR controller that picks the active level from BWE samples using a hysteresis-based step up/down algorithm.
- One audio track (same across all levels).
- Logging of BWE samples, level switches, and decisions.

Out of scope:
- Translation, TTS, or any commentary-specific logic.
- Multi-publisher or simulcast.
- Runtime video re-encoding.

## Folder layout

`ASSUMPTION-FOLDER: /home/ubuntu/bwe_experiment/` — sibling to `commentary/`, **not inside** the commentary repo.

```
/home/ubuntu/bwe_experiment/
├── README.md
├── CMakeLists.txt
├── config.json               # bitrate ladder, factors, durations, channel
├── prepare_content.sh        # ffmpeg pre-encode script
├── source_clip/              # copied from commentary, NOT symlinked
│   └── source.mp4
├── src/
│   ├── main.cpp              # entrypoint, Agora connection setup
│   ├── abr_controller.cpp/.h # BWE → level decision logic
│   ├── content_player.cpp/.h # reads H.264 + audio, pushes frames at real-time
│   ├── h264_reader.cpp/.h    # NAL parser, finds keyframes for switch points
│   └── logging.cpp/.h        # structured stderr + optional file logging
├── content/                  # gitignored — pre-encoded variants land here
│   ├── level0.h264
│   ├── level1.h264
│   ├── level2.h264
│   ├── level3.h264
│   ├── level4.h264
│   ├── level5.h264
│   └── audio.pcm             # 16kHz mono s16le
├── agora_sdk/                # gitignored — symlink or fresh extract
└── build/                    # gitignored
```

### Agora SDK — pure C++, no Go

**This experiment uses only the C++ Linux SDK.** No Go binding, no cgo, no dependency on the commentary project's Go publisher.

The shared library `libagora_rtc_sdk.so` is language-agnostic — the same binary that the commentary stack's Go process links via cgo. We link it directly from C++ here.

Version: **Agora-RTC-x86_64-linux-gnu v4.4.32** (current Agora release at time of writing). The existing extracted copy at `/home/ubuntu/commentary/go-audio-video-publisher/agora-sdk/agora_sdk/` already contains this version, including all C++ headers and the `.so` file.

**Two install options:**

Option A — **copy** from the existing extract (default, consistent with the "copy content" directive):

```bash
cp -r /home/ubuntu/commentary/go-audio-video-publisher/agora-sdk/agora_sdk \
      /home/ubuntu/bwe_experiment/agora_sdk
```

Option B — **fresh extract** from Agora's CDN (use this if a newer Agora release is available):

```bash
cd /home/ubuntu/bwe_experiment
# Download from Agora's download portal:
# https://www.agora.io/en/download/  (look for "RTC SDK for Linux x86_64")
tar xzf Agora-RTC-x86_64-linux-gnu-v4.4.32.tgz
mv agora_rtc_sdk/agora_sdk ./agora_sdk
rm -rf agora_rtc_sdk
```

Verify the install:

```bash
ls /home/ubuntu/bwe_experiment/agora_sdk/libagora_rtc_sdk.so
ls /home/ubuntu/bwe_experiment/agora_sdk/include/AgoraBase.h
```

The C++ source files in `src/` `#include` these headers directly. No Go binding code, no Cgo wrappers — `g++` builds a plain ELF binary that depends only on `libagora_rtc_sdk.so` and standard system libraries (`libpthread`, `libdl`, `libstdc++`).

**Source content is copied into the experiment folder**, not referenced from commentary, so the experiment is self-contained and editable in isolation:

```bash
mkdir -p /home/ubuntu/bwe_experiment/source_clip
cp /home/ubuntu/commentary/clips/bmg_fch_demo_5min/source.mp4 \
   /home/ubuntu/bwe_experiment/source_clip/source.mp4
```

`prepare_content.sh` then encodes the 6 levels from `source_clip/source.mp4` into `content/level0.h264` … `content/level5.h264`.

## Prerequisites

```bash
# System
sudo apt install cmake g++ ffmpeg pkg-config

# See "Agora SDK — pure C++, no Go" section below for SDK install (copy or fresh extract).
```

Credentials needed at runtime:
- Agora App ID (already in `commentary/.env` as `AGORA_APP_ID`)
- A channel name (e.g. `bwe_test`)
- A token (generate via `commentary/server/token_api.py` or any Agora token tool, or use App-ID-only mode in test)

## Content preparation

`prepare_content.sh` pre-encodes 6 H.264 variants from the source clip. Key constraints:

- **All levels share the same GOP size** (recommended **60 frames = 2 s at 30 fps**) so the publisher can switch on any keyframe boundary.
- **All levels share the same source frame rate** (30 fps).
- **Annex B raw H.264** output (no MP4 container — straight NAL bitstream the publisher can chunk and push directly via `PushVideoEncodedData`).
- Closed GOPs (no open-GOP B-frame references across keyframes) so switching mid-stream doesn't produce decode artifacts on the receiver.

`ASSUMPTION-SOURCE: source_clip/source.mp4` — copied verbatim from `commentary/clips/bmg_fch_demo_5min/source.mp4` into the experiment folder so the experiment is self-contained.

`ASSUMPTION-LADDER: 6 levels as below.` Confirm or override.

| Level | Resolution | Video bitrate | Approx pixels/s |
|---|---|---|---|
| 0 | 320 × 180 | 200 kbps | low |
| 1 | 480 × 270 | 400 kbps | |
| 2 | 640 × 360 | 700 kbps | |
| 3 | 960 × 540 | 1200 kbps | |
| 4 | 1280 × 720 | 2000 kbps | |
| 5 | 1280 × 720 | 3500 kbps | high |

`prepare_content.sh` outline:

```bash
#!/bin/bash
set -e
SRC="${1:-source_clip/source.mp4}"
OUT=content
mkdir -p "$OUT"

# Ladder: (level, width, height, bitrate)
LADDER=(
  "0 320 180 200k"
  "1 480 270 400k"
  "2 640 360 700k"
  "3 960 540 1200k"
  "4 1280 720 2000k"
  "5 1280 720 3500k"
)

for entry in "${LADDER[@]}"; do
  set -- $entry
  L=$1 W=$2 H=$3 BR=$4
  ffmpeg -y -i "$SRC" \
    -vf "scale=${W}:${H}" \
    -c:v libx264 -profile:v baseline -level 3.1 \
    -preset slow -tune zerolatency \
    -b:v $BR -maxrate $BR -bufsize $BR \
    -g 60 -keyint_min 60 -sc_threshold 0 \
    -bf 0 \
    -x264opts "no-open-gop" \
    -an \
    -f h264 "$OUT/level${L}.h264"
done

# One audio extraction (same for all levels)
ffmpeg -y -i "$SRC" \
  -vn -ar 16000 -ac 1 -f s16le \
  "$OUT/audio.pcm"

echo "Done. Levels:"
ls -lh "$OUT/"
```

Note: `-bf 0` (no B-frames) + closed GOP simplifies switching. Costs a small amount of efficiency vs B-frame-using encodes; acceptable for this experiment.

## ABR algorithm

### Inputs
- `onDownlinkNetworkInfoUpdated`: per remote receiver, `bwe_bps`, `lastmile_delay`, `expected_bitrate`
- Update rate: ~every 1–2 s per receiver
- Current published level: `current_level ∈ [0, 5]`

### State per receiver (uid)
- `bwe_window`: rolling window of (timestamp, bwe_bps) for the last `bwe_window_s` seconds (default **2 s**, configurable)
- `last_change_time`: timestamp of last level switch
- Per-receiver `rolling_avg_bwe` = arithmetic mean of all samples in `bwe_window`
- Per-receiver `instantaneous_bwe` = most recent BWE sample

### Decision rule (confirmed interpretation)

The algorithm compares the **instantaneous BWE** against the **rolling average BWE** over the recent window. It does **not** compare against the current bitrate-level target. This makes the controller react to BWE *trend changes* rather than to absolute bitrate fit. All thresholds and durations are configurable.

On each tick (default every `tick_interval_ms`, e.g. 500 ms):

```
# Aggregate across receivers (worst-case policy)
worst_instant_bwe = min over all receivers of receiver.instantaneous_bwe
worst_rolling_avg = min over all receivers of receiver.rolling_avg_bwe

# Downgrade check
if worst_instant_bwe < worst_rolling_avg / br_downgrade_factor:
    downgrade_sustained_seconds += tick_interval
    if downgrade_sustained_seconds >= br_downgrade_duration:
        if current_level > 0:
            switch_to(current_level - 1)
            downgrade_sustained_seconds = 0
            upgrade_sustained_seconds = 0   # reset opposing counter
else:
    downgrade_sustained_seconds = 0

# Upgrade check
# Multi-receiver rule: every receiver must satisfy the upgrade condition
elapsed_since_change = now - last_change_time
if elapsed_since_change > cooldown_after_switch:
    all_above = all(
        r.instantaneous_bwe > r.rolling_avg_bwe * br_upgrade_factor
        for r in receivers
    )
    if all_above:
        upgrade_sustained_seconds += tick_interval
        if upgrade_sustained_seconds >= br_upgrade_duration:
            if current_level < 5:
                switch_to(current_level + 1)
                upgrade_sustained_seconds = 0
                downgrade_sustained_seconds = 0   # reset opposing counter
    else:
        upgrade_sustained_seconds = 0
```

**Worked example.** With defaults `br_downgrade_factor=1.3`, `br_upgrade_factor=1.2`:

| Rolling avg (2 s) | Instantaneous BWE | Compare | Outcome |
|---|---|---|---|
| 1000 kbps | 750 kbps | 750 < 1000/1.3 = 769 → yes | downgrade timer accumulates |
| 1000 kbps | 850 kbps | 850 < 769 → no, 850 > 1200 → no | neither timer accumulates |
| 1000 kbps | 1250 kbps | 1250 > 1000*1.2 = 1200 → yes | upgrade timer accumulates |

The downgrade sustained period (default 3 s) protects against single-tick BWE dips; the upgrade sustained period (default 10 s) plus cooldown protects against bouncing immediately after a downgrade.

**Behaviour to be aware of.** Because the rule references only the BWE signal (not the level targets), the controller adapts to BWE *deltas* but does not, by itself, drive toward an "optimal" level for steady-state BWE. In practice, an over-bitrate publish will cause BWE to drop as packets back up at the receiver — the resulting drop is what triggers downgrade. Likewise an under-bitrate publish at a high-capacity link tends to produce rising BWE estimates as headroom is revealed, which the upgrade rule then acts on. Tune the factors and durations during testing if behaviour is too sluggish or too jittery.

### Switching mechanics

A switch from level A → level B happens at the next keyframe boundary in the *currently playing* file:

1. ABR controller signals desired level B.
2. Content player continues reading from A until it has just pushed the frame **before** the next keyframe in A.
3. At that point, it seeks file B to the matching wall-clock position (same source timeline offset) and reads from the next keyframe in B.
4. Pushes B's keyframe as the first frame of the new level.
5. No audio interruption — audio is a single shared file/stream, independent of video level.

Since all levels share GOP boundaries (every 60 frames = 2 s), the maximum delay between an ABR decision and the actual switch is one GOP (~2 s).

### Defaults (configurable in `config.json`)

```json
{
  "channel": "bwe_test",
  "publisher_uid": 1000,
  "frame_rate": 30,
  "gop_frames": 60,
  "audio_sample_rate": 16000,
  "audio_channels": 1,
  "levels": [
    {"id": 0, "width": 320, "height": 180, "bitrate_kbps": 200, "file": "content/level0.h264"},
    {"id": 1, "width": 480, "height": 270, "bitrate_kbps": 400, "file": "content/level1.h264"},
    {"id": 2, "width": 640, "height": 360, "bitrate_kbps": 700, "file": "content/level2.h264"},
    {"id": 3, "width": 960, "height": 540, "bitrate_kbps": 1200, "file": "content/level3.h264"},
    {"id": 4, "width": 1280, "height": 720, "bitrate_kbps": 2000, "file": "content/level4.h264"},
    {"id": 5, "width": 1280, "height": 720, "bitrate_kbps": 3500, "file": "content/level5.h264"}
  ],
  "audio_file": "content/audio.pcm",
  "start_level": 3,
  "abr": {
    "bwe_window_s": 2.0,
    "tick_interval_ms": 500,
    "br_downgrade_factor": 1.3,
    "br_downgrade_duration_s": 3.0,
    "br_upgrade_factor": 1.2,
    "br_upgrade_duration_s": 10.0,
    "cooldown_after_switch_s": 5.0
  },
  "log_file": "/tmp/bwe_publisher_stats.txt",
  "loop_content": true
}
```

`ASSUMPTION-DEFAULTS`: factor/duration values above are starting guesses based on typical ABR practice. Real numbers should be tuned during the experiment.

## Implementation outline

### File: `src/main.cpp`

Bootstrap pattern follows Agora's `sample_downlink_bwe.cpp`:

1. Parse CLI args: `--appid`, `--token`, `--channel`, `--uid`, `--config`, `--start-level`.
2. Load `config.json`.
3. Initialise Agora service and connection:
   - `IAgoraService::initialize(serviceConfig)`
   - `createRtcConnection(connectionConfig)` with `autoSubscribeVideo = true` (required for the network info events)
   - Register `IRtcConnectionObserver` (with `onUserJoined`, `onConnectionStateChanged`)
   - Register `INetworkObserver` (with `onDownlinkNetworkInfoUpdated`)
4. Create a video encoded image sender (`createVideoEncodedImageSender`) and an audio PCM sender (`createAudioPcmDataSender`).
5. Connect to the channel.
6. Start the content player thread.
7. Start the ABR controller tick thread.
8. Wait on a stop signal (SIGINT) and clean up.

### File: `src/h264_reader.cpp/.h`

- Opens a `level*.h264` Annex B file.
- Iterates NAL units (search for start codes 0x000001 / 0x00000001).
- Groups NAL units into frames (one access unit per push).
- Tags each frame as keyframe / delta by NAL type (5 = IDR for baseline).
- Exposes:
  - `next_frame()` → bytes + is_keyframe
  - `current_pts_ms()` → time offset from start of the file
  - `seek_to_keyframe_at_or_after(pts_ms)` → for level switches

### File: `src/content_player.cpp/.h`

A single thread reads from the **current level's** reader and pushes one frame per `1000/30 = 33.3 ms` via `PushVideoEncodedData`.

Audio sender pushes 10 ms PCM chunks at real-time from `audio.pcm` (separate cadence).

On a level-switch request from the ABR controller:
1. After pushing the current frame, peek the next frame.
2. If next frame is a keyframe, take the switch immediately: close current reader, open new level's reader, seek to keyframe at same wall-clock offset, push the new keyframe.
3. Otherwise, keep pushing from the current level until reaching the next keyframe boundary, then switch.

If `loop_content: true`, when a reader reaches EOF, re-open it from the start (matching wall-clock cycle).

Frame info pushed to Agora:

```c++
EncodedVideoFrameInfo info;
info.codecType = VIDEO_CODEC_H264;
info.width = current_level.width;
info.height = current_level.height;
info.framesPerSecond = 30;
info.frameType = is_keyframe ? VIDEO_FRAME_TYPE_KEY_FRAME : VIDEO_FRAME_TYPE_DELTA_FRAME;
info.captureTimeMs = pts_ms;
info.streamType = VIDEO_STREAM_HIGH;
```

### File: `src/abr_controller.cpp/.h`

Thread that:
- Receives `onDownlinkNetworkInfoUpdated` events (via shared queue / atomic ringbuffer from the SDK callback into this thread).
- Maintains per-uid `bwe_window`.
- Ticks every `tick_interval_ms`; applies the decision rule above.
- Signals content player via a thread-safe `set_target_level(int)`.

Receivers come and go (`onUserJoined`, `onUserLeft`). Maintain a map; prune entries on leave or after a stale timeout (e.g. no BWE update in 10 s).

### File: `src/logging.cpp/.h`

`LOG_INFO`, `LOG_WARN`, `LOG_DECISION` to stderr with timestamp. Every tick also appends a structured line to `log_file`.

**Live stats — printed to stderr every `tick_interval_ms` (default 500 ms):**

Per receiver (one line each):
```
[2026-05-19 21:30:42.123] tick uid=143784219 inst=1656000 avg2s=1500000 \
  inst_vs_avg_ratio=1.10 cur_level=4 down_susp=0.0 up_susp=7.5
```

Aggregate across receivers (driving the decision):
```
[2026-05-19 21:30:42.123] worst_inst=1500000 worst_avg=1450000 ratio_to_avg=1.03 \
  down_threshold=1115385 up_threshold=1740000 cur_level=4
```

On a level switch (decision lines, rare):
```
[2026-05-19 21:30:45.624] DECISION upgrade from L4 (2000kbps) to L5 (3500kbps) \
  reason="all receivers inst>avg*1.2 sustained 10.0s"
```

On receiver join/leave:
```
[2026-05-19 21:30:01.812] uid=143784219 joined (now tracking 1 receiver)
[2026-05-19 21:45:11.230] uid=143784219 left (now tracking 0 receivers)
```

This is enough to watch the algorithm running in real time in one terminal and reconstruct exactly why every switch happened. Pair with `tc qdisc` on the receiver host to manipulate bandwidth and observe the publisher react live.

## Build (`CMakeLists.txt` outline)

```cmake
cmake_minimum_required(VERSION 3.10)
project(bwe_publisher CXX)
set(CMAKE_CXX_STANDARD 17)

set(AGORA_SDK_DIR "${CMAKE_SOURCE_DIR}/agora_sdk")

include_directories(${AGORA_SDK_DIR}/include)
link_directories(${AGORA_SDK_DIR})

add_executable(bwe_publisher
  src/main.cpp
  src/abr_controller.cpp
  src/content_player.cpp
  src/h264_reader.cpp
  src/logging.cpp
)

target_link_libraries(bwe_publisher
  agora_rtc_sdk
  pthread
  dl
)

set(CMAKE_INSTALL_RPATH "$ORIGIN/../agora_sdk")
set_target_properties(bwe_publisher PROPERTIES
  BUILD_WITH_INSTALL_RPATH TRUE
)
```

Build steps:

```bash
cd /home/ubuntu/bwe_experiment
mkdir build && cd build
cmake .. && make -j
```

## Run

```bash
cd /home/ubuntu/bwe_experiment
export LD_LIBRARY_PATH="$PWD/agora_sdk"

./build/bwe_publisher \
  --appid "$AGORA_APP_ID" \
  --token "$AGORA_TEMP_TOKEN" \
  --channel bwe_test \
  --uid 1000 \
  --config config.json
```

## Testing

### Receiver side
Use any Agora client to subscribe to the same channel as a viewer:
- Web SDK demo (`viewer_live.html` from commentary repo, point at `bwe_test` channel + new token, would work)
- Mobile demo app
- Or a second C++ subscriber based on the Agora sample

### Triggering BWE changes
On the receiver host:

```bash
# Throttle inbound bandwidth to 500 kbps (forces downgrade)
sudo tc qdisc add dev eth0 root tbf rate 500kbit burst 10kb latency 50ms

# Restore
sudo tc qdisc del dev eth0 root
```

Watch publisher logs for level switches. Watch receiver to confirm resolution/bitrate actually changes.

### What to verify
- `onDownlinkNetworkInfoUpdated` fires with the receiver UID after they join.
- Without throttling, the publisher climbs to `start_level` then to the highest level that fits.
- Under throttling, the publisher steps down within `br_downgrade_duration` after the rolling average drops.
- After throttle removal, publisher waits `cooldown_after_switch` then climbs back up.
- Level switches don't cause perceptible decode glitches on the receiver (closed GOP + keyframe-aligned switch).
- Audio is continuous through every switch.

## Assumptions to confirm before implementation

Marked inline above with `ASSUMPTION-*` tags:

| Tag | Default | Confirm? |
|---|---|---|
| `ASSUMPTION-FOLDER` | `/home/ubuntu/bwe_experiment/` | Folder location for the experiment |
| `ASSUMPTION-SOURCE` | `commentary/clips/bmg_fch_demo_5min/source.mp4` | Source clip to encode |
| `ASSUMPTION-LADDER` | 6-level ladder shown above | Specific bitrates/resolutions |
| `ASSUMPTION-ALG-A` | **CONFIRMED**: "average" = rolling mean of BWE samples over last 2 s; compared against instantaneous BWE | — |
| `ASSUMPTION-DEFAULTS` | factors 1.3 / 1.2, durations 3 / 10 s, cooldown 5 s | ABR tuning starting values |

Any of these can be overridden in `config.json` at runtime without rebuilding.

## Open design choices for later iteration

- **Multi-receiver policy** — current rule is "downgrade to worst, upgrade only when all OK". Could be tuned to weighted average, percentile, or per-uid simulcast (Agora HIGH/LOW streams) once basic behaviour is working.
- **Audio bitrate adaptation** — currently fixed at 16 kHz mono. Likely unnecessary to adapt, but flagged.
- **Hysteresis tuning** — initial defaults are guesses. The log format above is designed to make tuning empirical: run, throttle, observe, adjust factors/durations, re-run.
- **Loop boundary** — when the source clip loops, all levels must wrap together. Easiest if they all share the same source timeline (since they came from the same MP4). Loop logic: when current file hits EOF, re-open from byte 0 and continue.

## Estimated effort

Solo C++ engineer with Agora SDK experience:

| Task | Estimate |
|---|---|
| Folder setup, SDK symlink, build skeleton | 0.5 d |
| `prepare_content.sh` and verify keyframe alignment across levels | 0.5 d |
| H264 NAL reader + keyframe detection | 1.0 d |
| Content player (publish at real-time, audio sync) | 1.0 d |
| ABR controller + per-uid BWE tracking | 1.0 d |
| Level switching mechanics (clean keyframe handoff) | 1.0 d |
| Logging, config parsing, CLI | 0.5 d |
| Integration test with throttled receiver + tuning | 1.0 d |
| **Total** | **6.5 d** |

## Reference materials

- Agora Linux Server SDK 4.4.32 — already extracted at `/home/ubuntu/commentary/go-audio-video-publisher/agora-sdk/agora_sdk/`
- Existing downlink BWE sample from Agora (`sample_downlink_bwe.cpp`) — provided by user; copy as starting point for `src/main.cpp`
- The commentary stack's `go-audio-video-publisher/main.go` shows the equivalent Go-side patterns (PushVideoEncodedData, PushAudioPcmData) — useful reference for parameter names

## Definition of done

- Standalone binary at `/home/ubuntu/bwe_experiment/build/bwe_publisher`.
- Publishes the football content to an Agora channel at the chosen start level.
- Receives BWE feedback from at least one subscriber.
- Demonstrably switches levels up and down under bandwidth manipulation.
- Log file shows every decision with its reason.
- No external runtime dependencies beyond the Agora SDK (no Python, no Go, no ffmpeg at runtime).
