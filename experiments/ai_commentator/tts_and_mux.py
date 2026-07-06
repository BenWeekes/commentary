#!/usr/bin/env python3
"""TTS each accepted commentary line via ElevenLabs, build an audio track that
mirrors realistic live-broadcast latency, mux with the source video.

Sync model:
  Each generated line has a `master_time_s` = video time of the LATEST frame
  in its 4-frame burst (relative to start of slice). Real-world live latency
  for THIS burst was its vision call latency + the TTS call latency.
  The audio for the line is scheduled at:

      play_at = video_time + vision_latency + tts_latency

  That mirrors what a live viewer would experience: source action visible at t,
  AI commentary heard ~(vision + tts) seconds later.

  If a later line's TTS would overlap a still-playing previous clip, we PUSH
  the later one to start right after the previous (no overlap, mirrors a
  single-mouth broadcaster). Lines pushed past the end of source are dropped.
"""
from __future__ import annotations
import io, json, os, struct, subprocess, time, wave
from pathlib import Path
import urllib.request, urllib.error

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
COMMENTARY = BASE / 'commentary.jsonl'
OUT_AUDIO_WAV = BASE / 'ai_commentary_track.wav'
OUT_MP4 = BASE / 'ai_commentary.mp4'
SOURCE_MP4 = Path('/tmp/v2v_compare/slice_5min.mp4')
DURATION_S = 300.0

# Manual env load
for line in open('/home/ubuntu/commentary/.env'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

EL_KEY = os.environ['ELEVENLABS_API_KEY']
# English sportscaster voice — fall back to default voice from .env if no
# dedicated EN sportscaster voice is configured.
EL_VOICE = os.environ.get('ELEVENLABS_VOICE_ID_EN_SPORTSCASTER') or os.environ['ELEVENLABS_VOICE_ID']
EL_MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_flash_v2_5')
SR_TTS = 16000  # we'll request pcm_16000 from ElevenLabs


def tts_one(text: str) -> tuple[bytes, int]:
    """Return (pcm_s16le_16k_mono_bytes, latency_ms) from ElevenLabs."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}?output_format=pcm_16000"
    body = json.dumps({
        "text": text,
        "model_id": EL_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": EL_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/pcm",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as r:
        pcm = r.read()
    return pcm, int((time.monotonic() - t0) * 1000)


def main():
    rows = [json.loads(l) for l in open(COMMENTARY)]
    accepted = [r for r in rows if r['accepted']]
    print(f"Loaded {len(rows)} bursts, {len(accepted)} accepted commentary lines")

    # TTS each line — sequential (we're building one audio track)
    clips = []  # (video_time_s, vision_ms, tts_ms, pcm_bytes, text)
    t0 = time.time()
    for i, r in enumerate(accepted):
        try:
            pcm, lat = tts_one(r['text'])
        except urllib.error.HTTPError as e:
            print(f"  TTS error on line {i}: HTTP {e.code} — skip")
            continue
        except Exception as e:
            print(f"  TTS error on line {i}: {e} — skip")
            continue
        clips.append({
            'video_time_s': r['video_time_s'],
            'vision_ms': r['vision_latency_ms'],
            'tts_ms': lat,
            'pcm': pcm,
            'text': r['text'],
            'duration_s': len(pcm) / 2 / SR_TTS,
        })
        if i % 25 == 0:
            print(f"  [{i}/{len(accepted)}] tts_lat={lat}ms dur={len(pcm)/2/SR_TTS:.2f}s "
                  f"elapsed={time.time()-t0:.0f}s text={r['text'][:60]!r}")
    print(f"TTS finished: {len(clips)} clips in {time.time()-t0:.1f}s")

    # Schedule clips: play_at = video_time + (vision_ms + tts_ms)/1000
    # then push later clips past previous clip's end (no overlap)
    silence_track = bytearray(int(DURATION_S * SR_TTS * 2))  # 5 min of silence
    dropped_overlap = 0
    last_end_s = 0.0
    scheduled = []
    for c in clips:
        realistic_lag_s = (c['vision_ms'] + c['tts_ms']) / 1000.0
        desired_start_s = c['video_time_s'] + realistic_lag_s
        # avoid overlap
        start_s = max(desired_start_s, last_end_s)
        end_s = start_s + c['duration_s']
        if start_s >= DURATION_S:
            dropped_overlap += 1
            continue
        # paste pcm
        start_byte = int(start_s * SR_TTS) * 2
        usable = min(len(c['pcm']), len(silence_track) - start_byte)
        if usable > 0:
            silence_track[start_byte:start_byte + usable] = c['pcm'][:usable]
        c['scheduled_start_s'] = round(start_s, 3)
        c['scheduled_end_s'] = round(end_s, 3)
        c['realistic_lag_s'] = round(realistic_lag_s, 3)
        scheduled.append(c)
        last_end_s = end_s

    print(f"Scheduled: {len(scheduled)} clips, dropped past end: {dropped_overlap}")

    # Write the audio track as WAV
    with wave.open(str(OUT_AUDIO_WAV), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR_TTS)
        w.writeframes(bytes(silence_track))
    print(f"Wrote {OUT_AUDIO_WAV}")

    # Mux: original video + AI commentary audio (drop original audio)
    subprocess.run([
        'ffmpeg', '-y',
        '-i', str(SOURCE_MP4),
        '-i', str(OUT_AUDIO_WAV),
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '96k',
        '-shortest',
        str(OUT_MP4),
    ], check=True, capture_output=True)
    print(f"Wrote {OUT_MP4}")

    # Also a side-by-side: original audio LEFT, AI commentary RIGHT
    sbs = BASE / 'ai_commentary_sidebyside.mp4'
    subprocess.run([
        'ffmpeg', '-y',
        '-i', str(SOURCE_MP4),
        '-i', str(OUT_AUDIO_WAV),
        '-filter_complex',
        '[0:a]channelsplit=channel_layout=mono:channels=FC[ol];'
        '[1:a]aformat=channel_layouts=mono[gr];'
        '[ol][gr]join=inputs=2:channel_layout=stereo[a]',
        '-map', '0:v:0', '-map', '[a]',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
        str(sbs),
    ], check=True, capture_output=True)
    print(f"Wrote {sbs}")

    # Save scheduled list for the results page
    sched_path = BASE / 'commentary_scheduled.jsonl'
    with open(sched_path, 'w') as f:
        for c in scheduled:
            f.write(json.dumps({k: v for k, v in c.items() if k != 'pcm'}) + '\n')
    print(f"Wrote {sched_path}")

    # Latency summary
    lats_vision = sorted(c['vision_ms'] for c in scheduled)
    lats_tts = sorted(c['tts_ms'] for c in scheduled)
    lats_total = sorted(int(c['realistic_lag_s'] * 1000) for c in scheduled)
    def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
    print(f"\n=== Latency ===")
    print(f"vision: p50={pct(lats_vision,0.5)}ms p90={pct(lats_vision,0.9)}ms max={max(lats_vision)}ms")
    print(f"tts:    p50={pct(lats_tts,0.5)}ms p90={pct(lats_tts,0.9)}ms max={max(lats_tts)}ms")
    print(f"total:  p50={pct(lats_total,0.5)}ms p90={pct(lats_total,0.9)}ms max={max(lats_total)}ms")


if __name__ == '__main__':
    main()
