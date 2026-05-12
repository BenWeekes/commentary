#!/bin/bash
# Build a latency test clip: black video with seconds counter + spoken
# "Mark N seconds" markers every INTERVAL seconds.

set -e

DURATION="${DURATION:-300}"
INTERVAL="${INTERVAL:-5}"
OUT="${OUT:-clips/latency_test/source.mp4}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

N=$((DURATION/INTERVAL))
echo "Building $N markers over ${DURATION}s into $OUT"

# 1) Generate per-marker WAVs and pad each to exactly INTERVAL seconds.
#    Block 0 = leading silence; block i contains marker announcing time i*INTERVAL.
ffmpeg -y -f lavfi -i "anullsrc=r=16000:cl=mono" -t $INTERVAL \
    -ar 16000 -ac 1 -c:a pcm_s16le "$TMP/block_0.wav" -loglevel error

for i in $(seq 1 $N); do
    t=$((i*INTERVAL))
    # raw marker speech
    espeak-ng -v en-us+m3 -s 150 -p 50 -w "$TMP/m${i}.wav" "Mark ${t} seconds." 2>/dev/null
    # pad/clip to exactly INTERVAL seconds at 16k mono
    ffmpeg -y -i "$TMP/m${i}.wav" \
        -af "aresample=16000,apad=whole_dur=${INTERVAL},atrim=end=${INTERVAL}" \
        -ar 16000 -ac 1 -c:a pcm_s16le "$TMP/block_${i}.wav" -loglevel error
done

# 2) Concat list
{
    for i in $(seq 0 $N); do
        echo "file 'block_${i}.wav'"
    done
} > "$TMP/concat.txt"

ffmpeg -y -f concat -safe 0 -i "$TMP/concat.txt" \
    -ar 16000 -ac 1 -c:a pcm_s16le "$TMP/audio.wav" -loglevel error

# 3) Video: black with two text overlays
ffmpeg -y -f lavfi -i "color=c=black:s=1280x720:r=25" -t $DURATION \
    -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:\
text='T=%{eif\\:t\\:d}s':fontcolor=white:fontsize=140:x=(w-text_w)/2:y=h/2-160,\
drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:\
text='MARK %{eif\\:floor(t/${INTERVAL})*${INTERVAL}\\:d}':\
fontcolor=yellow:fontsize=100:x=(w-text_w)/2:y=h/2+40" \
    -c:v libx264 -preset fast -pix_fmt yuv420p -g 50 -profile:v baseline -level 3.1 \
    -movflags +faststart "$TMP/video.mp4" -loglevel error

# 4) Mux
ffmpeg -y -i "$TMP/video.mp4" -i "$TMP/audio.wav" \
    -c:v copy -c:a aac -b:a 96k -shortest \
    -movflags +faststart "$OUT" -loglevel error

ls -lh "$OUT"
echo "done."
