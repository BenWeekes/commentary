#!/usr/bin/env python3
"""Mux commentary WAV + source-video crowd bed into a single MP4.

The source MP4 has the original broadcast audio, which contains crowd noise
(and the human commentary). We can't isolate crowd cleanly, but we can:

  1. Extract the source audio, low-pass filter it aggressively to keep the
     crowd (broadband) and dampen the speech energy, THEN attenuate it further
     to about -22 dB relative to full scale.
  2. Mix the commentary at 0 dB with that atmospheric bed.

The result: AI commentary sits on top of a live-feel crowd bed. Human
commentary from the original leaks through faintly but is barely audible
against the AI commentary.

Usage:
  python mux_with_crowd.py <source_mp4> <commentary_wav> <out_mp4>
"""
import subprocess, sys

def mux_with_crowd(source_mp4, commentary_wav, out_mp4,
                   commentary_gain_db=0.0, crowd_gain_db=-22.0):
    # ffmpeg filter graph:
    #   [0:a]  source audio → highpass 300 Hz to drop rumble/low speech
    #                       → aformat mono → volume adjust → [crowd]
    #   [1:a]  commentary wav → aformat mono → volume adjust → [comm]
    #   [comm][crowd]amix     → [mix]
    # video is copied through untouched.
    filter_str = (
        f"[0:a]highpass=f=250,aformat=channel_layouts=mono,volume={crowd_gain_db}dB[crowd];"
        f"[1:a]aformat=channel_layouts=mono,volume={commentary_gain_db}dB[comm];"
        f"[comm][crowd]amix=inputs=2:duration=first:dropout_transition=0[mix]"
    )
    cmd = [
        'ffmpeg', '-y',
        '-i', source_mp4,
        '-i', commentary_wav,
        '-filter_complex', filter_str,
        '-map', '0:v:0',
        '-map', '[mix]',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest',
        out_mp4,
        '-loglevel', 'error',
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(r.stderr.decode(errors='replace')[:400])
        raise RuntimeError(f"ffmpeg failed: {r.returncode}")
    return out_mp4


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(__doc__); sys.exit(1)
    mux_with_crowd(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"wrote {sys.argv[3]}")
