#!/usr/bin/env python3
"""Tier A milestone 1 (plan_tracking.md): YOLOv8n over the stored 5-min master
frames -> per-frame JSON with player bboxes, kit-colour 2-team clustering, ball
position, and an approximate on-pitch filter. No OCR yet (that's milestone 4).

Runs on the T4 (device=0). Emits tracking_tier_a.json (one record per frame) +
prints a 10-frame spot-check.

Usage:
  /home/ubuntu/commentary/.venv-track/bin/python track_tier_a.py
"""
import json, sys
from pathlib import Path
import numpy as np
import cv2
from ultralytics import YOLO

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES_DIR = BASE / 'frames'
OUT = BASE / 'tracking_tier_a.json'
SAMPLE_INTERVAL_S = 0.55
PERSON, SPORTS_BALL = 0, 32          # COCO class ids
CONF = 0.25

# --- helpers ---------------------------------------------------------------

def zone_of(cx, cy):
    col = 'left' if cx < 0.34 else ('right' if cx > 0.66 else 'mid')
    row = 'far' if cy < 0.5 else 'near'
    return f"{row}-{col}"


def torso_kit_feature(img_bgr, box):
    """Mean HSV of the torso region, ignoring green pitch, white lines and
    very dark pixels. Returns [H, S, V] or None if nothing usable."""
    x1, y1, x2, y2 = [int(v) for v in box]
    h = y2 - y1
    ty1, ty2 = y1 + int(0.15 * h), y1 + int(0.55 * h)   # torso band
    cx1 = x1 + int(0.20 * (x2 - x1)); cx2 = x2 - int(0.20 * (x2 - x1))
    crop = img_bgr[max(0, ty1):ty2, max(0, cx1):cx2]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    green = (H > 35) & (H < 85) & (S > 40)               # pitch
    dark = V < 40
    keep = ~(green | dark)
    if keep.sum() < 10:
        keep = ~dark
    if keep.sum() < 5:
        return None
    hue = H[keep]
    # circular mean of hue (OpenCV hue is 0..179 -> scale to 0..2pi)
    ang = hue / 180.0 * 2 * np.pi
    mh = (np.arctan2(np.sin(ang).mean(), np.cos(ang).mean()) % (2 * np.pi)) / (2 * np.pi) * 180.0
    return [float(mh), float(S[keep].mean()), float(V[keep].mean())]


def kmeans2(feats, iters=25):
    """Tiny 2-cluster k-means on kit features (hue as circular via cos/sin)."""
    X = np.array([[np.cos(f[0]/180*2*np.pi), np.sin(f[0]/180*2*np.pi),
                   f[1]/255.0, f[2]/255.0] for f in feats], dtype=np.float32)
    rng = np.random.default_rng(0)
    c = X[rng.choice(len(X), 2, replace=False)]
    lab = np.zeros(len(X), int)
    for _ in range(iters):
        d = np.linalg.norm(X[:, None] - c[None], axis=2)
        newlab = d.argmin(1)
        if (newlab == lab).all():
            break
        lab = newlab
        for k in (0, 1):
            if (lab == k).any():
                c[k] = X[lab == k].mean(0)
    return lab, c


def hue_name(h, s):
    if s < 45:
        return 'white/light'
    if h < 12 or h > 168:
        return 'red'
    if h < 25:
        return 'orange'
    if h < 35:
        return 'yellow'
    if h < 85:
        return 'green'
    if h < 130:
        return 'blue'
    return 'purple/pink'


# --- main ------------------------------------------------------------------

def main():
    frames = sorted(FRAMES_DIR.glob('f_*.jpg'))
    if not frames:
        print(f"no frames in {FRAMES_DIR}"); sys.exit(1)
    print(f"loading YOLOv8n; running over {len(frames)} frames on GPU...")
    model = YOLO('yolov8n.pt')

    # Pass 1: detect, collect person kit features globally for consistent teams
    per_frame = []
    all_feats = []
    for i, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        H, W = img.shape[:2]
        r = model.predict(img, classes=[PERSON, SPORTS_BALL], conf=CONF,
                          verbose=False, device=0)[0]
        # green ratio of lower half = crude "is this a pitch shot"
        hsv = cv2.cvtColor(img[H//2:], cv2.COLOR_BGR2HSV)
        green_ratio = float(((hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 85) &
                             (hsv[:, :, 1] > 40)).mean())
        players, ball = [], None
        for b in r.boxes:
            cls = int(b.cls); conf = float(b.conf)
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            bw, bh = x2 - x1, y2 - y1
            if cls == SPORTS_BALL:
                if ball is None or conf > ball['conf']:
                    cx, cy = (x1 + x2) / 2 / W, (y1 + y2) / 2 / H
                    ball = {'conf': round(conf, 2), 'cx': round(cx, 3),
                            'cy': round(cy, 3), 'zone': zone_of(cx, cy)}
                continue
            # person size filter (drop tiny crowd specks / huge foreground)
            if bh < 18 or bh > 0.9 * H or bw > 0.5 * W:
                continue
            feat = torso_kit_feature(img, (x1, y1, x2, y2))
            fidx = len(all_feats) if feat is not None else -1
            if feat is not None:
                all_feats.append(feat)
            # on-pitch: green dominant just below the feet
            fy = min(H - 1, int(y2 + 3)); fx = int((x1 + x2) / 2)
            patch = cv2.cvtColor(img[max(0, fy-4):fy+4, max(0, fx-8):fx+8],
                                 cv2.COLOR_BGR2HSV)
            on_pitch = bool(patch.size and ((patch[:, :, 0] > 35) &
                            (patch[:, :, 0] < 85) & (patch[:, :, 1] > 40)).mean() > 0.3)
            players.append({'bbox': [round(x1), round(y1), round(x2), round(y2)],
                            'conf': round(conf, 2), 'on_pitch': on_pitch,
                            '_feat_idx': fidx})
        per_frame.append({'frame': fp.name,
                          'video_time_s': round((i + 1) * SAMPLE_INTERVAL_S, 2),
                          'green_ratio': round(green_ratio, 2),
                          'ball': ball, 'players': players})
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(frames)} frames")

    # Global 2-team clustering on all torso features
    team_labels, centroids = kmeans2(all_feats) if len(all_feats) >= 2 else ([], None)
    team_names = {}
    if centroids is not None:
        for k in (0, 1):
            ang = np.arctan2(centroids[k][1], centroids[k][0]) % (2*np.pi)
            hh = ang/(2*np.pi)*180; ss = centroids[k][2]*255
            team_names[k] = hue_name(hh, ss)

    # Pass 2: assign teams, strip temp indices
    for rec in per_frame:
        counts = {0: 0, 1: 0}
        for p in rec['players']:
            fi = p.pop('_feat_idx')
            if fi >= 0 and len(team_labels):
                t = int(team_labels[fi]); p['team'] = t; counts[t] += 1
            else:
                p['team'] = None
        rec['counts'] = {f'team{k}': counts[k] for k in (0, 1)}

    out = {'meta': {'frames': len(frames), 'sample_interval_s': SAMPLE_INTERVAL_S,
                    'model': 'yolov8n.pt (COCO)',
                    'teams': {f'team{k}': team_names.get(k, '?') for k in (0, 1)},
                    'note': 'Tier A milestone 1 — bboxes + kit clusters + ball; no jersey OCR yet'},
           'frames': per_frame}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    tot_players = sum(len(r['players']) for r in per_frame)
    tot_ball = sum(1 for r in per_frame if r['ball'])
    print(f"\nwrote {OUT}")
    print(f"teams: {out['meta']['teams']}")
    print(f"total player detections: {tot_players}   frames with ball: {tot_ball}/{len(frames)}")
    print("\n=== spot-check (10 frames spread across the clip) ===")
    for j in range(0, len(per_frame), max(1, len(per_frame)//10))[:10]:
        r = per_frame[j]
        b = r['ball']
        bstr = f"ball {b['zone']}@{b['conf']}" if b else "no ball"
        print(f"  {r['frame']} t={r['video_time_s']:6.1f}s green={r['green_ratio']:.2f} "
              f"players={len(r['players'])} (t0={r['counts']['team0']},t1={r['counts']['team1']}) {bstr}")


if __name__ == '__main__':
    main()
