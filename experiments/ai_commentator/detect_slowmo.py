#!/usr/bin/env python3
"""Offline slow-mo detector — computes per-frame motion across the 5-min slice
and identifies segments with unusually low motion (candidate replays).

Method:
  1. Load each master frame (960x540 JPEG), downsample to 128x72 grayscale.
  2. Compute mean absolute pixel difference vs the PREVIOUS frame.
  3. Rolling window baseline (median of last 20 frames' motion).
  4. Flag a frame as SLOW when its motion < 40% of baseline for >= 3 frames in a row.
  5. Merge adjacent slow spans into segments; report those >= 2 s.

Cost: ~10 ms per frame, ~5 s total for 545 frames. No real-time constraint —
this is an offline evaluation aid.
"""
from __future__ import annotations
import numpy as np
from PIL import Image
from pathlib import Path
import json, sys

FRAMES_DIR = Path('/home/ubuntu/commentary/experiments/ai_commentator/frames')
SAMPLE_INTERVAL_S = 0.55

# Downsample target
DS_W, DS_H = 128, 72

# Detection knobs
SLOW_THRESH_RATIO = 0.40    # frame's motion must be <40% of local baseline
SLOW_MIN_CONSEC = 3          # need >=3 frames in a row below threshold
MIN_SEGMENT_S = 2.0          # only report segments >= 2 seconds
BASELINE_WINDOW = 20         # frames of rolling median


def load_gray(path):
    with Image.open(path) as img:
        img = img.convert('L').resize((DS_W, DS_H), Image.BILINEAR)
        return np.asarray(img, dtype=np.int16)


def main():
    frames = sorted(FRAMES_DIR.glob('f_*.jpg'))
    print(f"Analysing {len(frames)} frames at {SAMPLE_INTERVAL_S}s intervals ({len(frames)*SAMPLE_INTERVAL_S:.0f}s of video)")

    # Compute per-frame motion vs previous
    motion = [0.0]  # first frame has no previous
    prev = load_gray(frames[0])
    for f in frames[1:]:
        cur = load_gray(f)
        m = float(np.abs(cur - prev).mean())
        motion.append(m)
        prev = cur

    m_arr = np.array(motion)
    print(f"Motion: min={m_arr.min():.1f} p25={np.percentile(m_arr,25):.1f} "
          f"median={np.median(m_arr):.1f} p75={np.percentile(m_arr,75):.1f} max={m_arr.max():.1f}")

    # Rolling median baseline
    baseline = np.zeros_like(m_arr)
    for i in range(len(m_arr)):
        lo = max(0, i - BASELINE_WINDOW)
        hi = min(len(m_arr), i + BASELINE_WINDOW + 1)
        baseline[i] = np.median(m_arr[lo:hi])

    # Ratio to baseline
    ratio = m_arr / np.where(baseline > 1, baseline, 1)

    # Flag slow frames
    is_slow = ratio < SLOW_THRESH_RATIO

    # Find runs of consecutive slow frames
    segments = []
    run_start = None
    for i, s in enumerate(is_slow):
        if s:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start >= SLOW_MIN_CONSEC:
                segments.append((run_start, i - 1))
            run_start = None
    if run_start is not None and len(is_slow) - run_start >= SLOW_MIN_CONSEC:
        segments.append((run_start, len(is_slow) - 1))

    # Convert to time and filter
    detected = []
    for start_idx, end_idx in segments:
        start_s = start_idx * SAMPLE_INTERVAL_S
        end_s = end_idx * SAMPLE_INTERVAL_S
        dur = end_s - start_s
        if dur >= MIN_SEGMENT_S:
            avg_ratio = float(ratio[start_idx:end_idx+1].mean())
            detected.append({
                'start_s': round(start_s, 1),
                'end_s': round(end_s, 1),
                'duration_s': round(dur, 1),
                'avg_ratio_to_baseline': round(avg_ratio, 2),
            })

    print(f"\n=== Detected {len(detected)} candidate slow-mo segments (>= {MIN_SEGMENT_S}s, motion < {SLOW_THRESH_RATIO*100:.0f}% of local baseline) ===")
    for d in detected:
        print(f"  {d['start_s']:6.1f}s - {d['end_s']:6.1f}s ({d['duration_s']:.1f}s)  "
              f"motion={d['avg_ratio_to_baseline']*100:.0f}% of baseline")

    # Also dump the per-frame data for anyone who wants to plot it
    out = {
        'frame_count': len(frames),
        'sample_interval_s': SAMPLE_INTERVAL_S,
        'motion_per_frame': [round(x, 2) for x in motion],
        'baseline_per_frame': [round(x, 2) for x in baseline],
        'ratio_per_frame': [round(x, 3) for x in ratio],
        'detected_segments': detected,
        'knobs': {
            'slow_thresh_ratio': SLOW_THRESH_RATIO,
            'slow_min_consec': SLOW_MIN_CONSEC,
            'min_segment_s': MIN_SEGMENT_S,
            'baseline_window': BASELINE_WINDOW,
        },
    }
    out_path = Path('/home/ubuntu/commentary/experiments/ai_commentator/motion_analysis.json')
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == '__main__':
    main()
