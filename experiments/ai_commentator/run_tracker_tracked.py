#!/usr/bin/env python3
"""Tracked player identification — the upgrade over per-frame detection.

Instead of OCR'ing every frame independently, we run a real multi-object
tracker (BoT-SORT) so each player holds a PERSISTENT id across frames, then:

  1. OCR a track opportunistically (only while it is still unnamed, only on
     big-enough boxes, only every few frames — the number does NOT need to be
     readable every frame).
  2. VOTE the number over the track's lifetime (sum of OCR confidence per digit).
  3. PROPAGATE the winning identity to EVERY frame of that track — including the
     frames where the back was turned / it was blurred / occluded.

Headline metric: fraction of TRACKS we can put a name to (identity coverage),
plus how many player-frames end up identified once propagated — the thing that
per-frame OCR read-rate undersells.

Usage:
  .venv-track/bin/python run_tracker_tracked.py <video.mp4> [vid_stride=10] [--json out.json]
"""
import sys, json, collections
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_tracker_detector import read_number, MODELS
from ultralytics import YOLO
import easyocr

MIN_BOX_H = 34        # px — don't OCR tiny distant players
OCR_EVERY = 3         # OCR an unnamed track at most every Nth tracked frame it appears in
NAME_CONF_SUM = 1.2   # a track is "named" once its best digit's summed OCR conf clears this
MIN_LIFETIME = 5      # frames — ignore fleeting tracks (false detections)


def run(video, vid_stride=10, tracker='botsort.yaml'):
    model = YOLO(str(MODELS / 'football-player-detection.pt'))
    reader = easyocr.Reader(['en'], gpu=True)

    votes = collections.defaultdict(lambda: collections.defaultdict(float))  # tid -> {num: conf_sum}
    region_hits = collections.Counter()
    lifetime = collections.Counter()          # tid -> frames seen
    seen_since_ocr = collections.Counter()     # tid -> frames since last OCR
    frames = 0
    player_frames = 0                          # total player detections across frames
    res_wh = None

    results = model.track(source=str(video), stream=True, persist=True, tracker=tracker,
                          classes=[1, 2], conf=0.35, vid_stride=vid_stride, device=0, verbose=False)
    for r in results:
        frames += 1
        if res_wh is None:
            res_wh = [int(r.orig_shape[1]), int(r.orig_shape[0])]
        if r.boxes is None or r.boxes.id is None:
            continue
        img = r.orig_img
        xy = r.boxes.xyxy.cpu().numpy()
        ids = r.boxes.id.cpu().numpy().astype(int)
        for box, tid in zip(xy, ids):
            lifetime[tid] += 1
            player_frames += 1
            bh = box[3] - box[1]
            named = max(votes[tid].values()) >= NAME_CONF_SUM if votes[tid] else False
            seen_since_ocr[tid] += 1
            if (not named) and bh >= MIN_BOX_H and seen_since_ocr[tid] >= OCR_EVERY:
                seen_since_ocr[tid] = 0
                num, conf, region = read_number(reader, img, box)
                if num is not None:
                    votes[tid][num] += conf
                    region_hits[region] += 1

    # resolve identities over tracks that actually persisted
    tracks = [tid for tid, n in lifetime.items() if n >= MIN_LIFETIME]
    named = {}
    for tid in tracks:
        if votes[tid]:
            best = max(votes[tid].items(), key=lambda kv: kv[1])
            if best[1] >= NAME_CONF_SUM:
                named[tid] = {'number': best[0], 'conf_sum': round(best[1], 2)}
    # how many player-frames get an identity once propagated across the track
    identified_frames = sum(lifetime[tid] for tid in named)

    return {
        'video': Path(video).name, 'res': res_wh, 'tracker': tracker,
        'frames_processed': frames, 'vid_stride': vid_stride,
        'tracks_persisted': len(tracks),
        'tracks_named': len(named),
        'track_name_rate': round(len(named) / len(tracks), 3) if tracks else 0.0,
        'player_frames': player_frames,
        'identified_player_frames': identified_frames,
        'frame_identity_coverage': round(identified_frames / player_frames, 3) if player_frames else 0.0,
        'ocr_region_hits': dict(region_hits),
        'named_numbers': sorted(v['number'] for v in named.values()),
    }


if __name__ == '__main__':
    video = sys.argv[1]
    vid_stride = int(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else 10
    out = None
    if '--json' in sys.argv:
        out = sys.argv[sys.argv.index('--json') + 1]
    res = run(video, vid_stride)
    print(json.dumps(res, indent=2))
    if out:
        Path(out).write_text(json.dumps(res, indent=2))
