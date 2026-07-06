#!/bin/bash
# Mux original video with Gemini's French audio into a watchable MP4.
# Also includes a hardcoded subtitle track from the Gemini FR transcript.
set -e

BASE=/tmp/v2v_compare
cd "$BASE"

# 1. Convert Gemini 24kHz mono to 48kHz stereo AAC (broader player support)
ffmpeg -y -i gemini_fr_audio.wav -ar 48000 -ac 2 -c:a aac -b:a 96k gemini_fr_audio.aac -loglevel error

# 2. Mux original 5min video + Gemini French audio (drop original audio)
ffmpeg -y -i slice_5min.mp4 -i gemini_fr_audio.aac \
    -map 0:v:0 -map 1:a:0 -c:v copy -c:a copy -shortest \
    gemini_5min.mp4 -loglevel error

echo "Created: $BASE/gemini_5min.mp4 ($(du -h gemini_5min.mp4 | cut -f1))"
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1 gemini_5min.mp4

# 3. Also make a side-by-side: original audio left channel, Gemini right
ffmpeg -y -i slice_5min.mp4 -i gemini_fr_audio.aac \
    -filter_complex "[0:a]channelsplit=channel_layout=mono:channels=FC[ol];[1:a]aformat=channel_layouts=mono[gr];[ol][gr]join=inputs=2:channel_layout=stereo[a]" \
    -map 0:v:0 -map "[a]" -c:v copy -c:a aac -b:a 128k \
    gemini_5min_sidebyside.mp4 -loglevel error
echo "Created: $BASE/gemini_5min_sidebyside.mp4 (original audio left, Gemini French right)"
