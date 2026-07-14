#!/usr/bin/env python3
"""Tracker column for the vision-eval page — roboflow/sports YOLO weights
(player + ball) + kit-colour team + easyOCR jersey numbers, emitted in the SAME
events_detector schema as the vision models so it lines up as a comparable column.

Honest scope: the tracker fills possession (team / side / shirt#) + ball_state.
It does NOT infer discrete events (foul/card/etc.) or attack direction, so
events=[] and third="unknown". Its whole value here is: can a detector read the
ball-carrier's team + shirt number better than an LLM?

Runs on the T4 via .venv-track.
Usage:
  /home/ubuntu/commentary/.venv-track/bin/python run_tracker_detector.py [--stride 2] [--limit N]
"""
import argparse, json, time, math
from pathlib import Path
import numpy as np, cv2
from ultralytics import YOLO
import easyocr

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
FRAMES = BASE / 'frames'
MODELS = BASE / 'tracker_models'
OUT = BASE / 'events_tracker.jsonl'
SAMPLE_INTERVAL_S = 0.55
BURST = 4
PITCH_LEN = 12000.0
# roboflow SoccerPitchConfiguration.vertices (cm), in pitch-detection keypoint order
PITCH_VERTS = np.array([(0,0),(0,1450),(0,2584),(0,4416),(0,5550),(0,7000),(550,2584),(550,4416),
    (1100,3500),(2015,1450),(2015,2584),(2015,4416),(2015,5550),(6000,0),(6000,2585),(6000,4415),
    (6000,7000),(9985,1450),(9985,2584),(9985,4416),(9985,5550),(10900,3500),(11450,2584),(11450,4416),
    (12000,0),(12000,1450),(12000,2584),(12000,4416),(12000,5550),(12000,7000),(5085,3500),(6915,3500)],
    dtype=np.float32)


def kit_team(img, box):
    """Return 'home' (Mainz red) / 'away' (Union olive) / 'unknown' from torso hue."""
    x1, y1, x2, y2 = [int(v) for v in box]
    h = y2 - y1
    crop = img[y1 + int(0.15*h):y1 + int(0.55*h), x1:x2]
    if crop.size == 0:
        return 'unknown'
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    keep = (S > 45) & (V > 40) & ~((H > 35) & (H < 85) & (S > 40))  # drop pitch green + dull
    if keep.sum() < 8:
        return 'unknown'
    hue = H[keep]
    red = ((hue < 12) | (hue > 168)).mean()
    olive = ((hue >= 25) & (hue <= 45)).mean()   # olive/gold-green
    if red > 0.35 and red > olive:
        return 'home'
    if olive > 0.30 and olive > red:
        return 'away'
    return 'home' if red >= olive else 'away'


def read_number(reader, img, box):
    """OCR a player's number from BOTH the shirt (upper back) and the shorts —
    the number is printed on both, and whichever faces the camera / is less
    occluded reads better. Returns (number|None, confidence, region)."""
    x1, y1, x2, y2 = [int(v) for v in box]
    h, w = y2 - y1, x2 - x1
    if h < 10 or w < 6:
        return None, 0.0, None
    regions = {
        'shirt':  img[y1 + int(0.12*h):y1 + int(0.52*h), x1 + int(0.15*w):x2 - int(0.15*w)],
        'shorts': img[y1 + int(0.55*h):y1 + int(0.82*h), x1 + int(0.08*w):x2 - int(0.08*w)],
    }
    best = None; bconf = 0.0; breg = None
    for reg, crop in regions.items():
        if crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
            continue
        crop = cv2.resize(crop, (crop.shape[1]*3, crop.shape[0]*3), interpolation=cv2.INTER_CUBIC)
        for _, txt, conf in reader.readtext(crop, allowlist='0123456789', detail=1, text_threshold=0.5):
            t = ''.join(ch for ch in txt if ch.isdigit())
            if t and 1 <= len(t) <= 2 and conf > bconf:
                best, bconf, breg = int(t), conf, reg
    return best, bconf, breg


def side_of(cx):
    return 'left' if cx < 0.34 else ('right' if cx > 0.66 else 'centre')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stride', type=int, default=2)
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    player_model = YOLO(str(MODELS / 'football-player-detection.pt'))
    ball_model = YOLO(str(MODELS / 'football-ball-detection.pt'))
    pitch_model = YOLO(str(MODELS / 'football-pitch-detection.pt'))
    reader = easyocr.Reader(['en'], gpu=True)
    orient = []  # (kit_team, ball-length-fraction) from goalkeepers → clip orientation
    frames = sorted(FRAMES.glob('f_*.jpg'))
    idxs = list(range(BURST - 1, len(frames), args.stride))
    if args.limit:
        idxs = idxs[:args.limit]
    print(f"tracker: {len(idxs)} bursts (stride {args.stride}) over {len(frames)} frames")

    out = []
    t0 = time.time()
    for k, i in enumerate(idxs):
        fp = frames[i]; img = cv2.imread(str(fp)); Hh, Ww = img.shape[:2]
        tt = time.monotonic()
        pr = player_model.predict(img, conf=0.35, verbose=False, device=0)[0]
        br = ball_model.predict(img, conf=0.25, verbose=False, device=0)[0]
        # green ratio → pitch vs non-pitch
        hsv = cv2.cvtColor(img[Hh//2:], cv2.COLOR_BGR2HSV)
        green = float(((hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 40)).mean())
        # player model classes: 0=ball 1=goalkeeper 2=player 3=referee
        players, keepers, refs = [], [], 0
        for b in pr.boxes:
            c = int(b.cls)
            if c == 2: players.append(b.xyxy[0].tolist())
            elif c == 1: keepers.append(b.xyxy[0].tolist())
            elif c == 3: refs += 1
        carrier_pool = players + keepers
        ball = None
        if len(br.boxes):
            bb = max(br.boxes, key=lambda b: float(b.conf))
            bx = bb.xyxy[0].tolist(); ball = (((bx[0]+bx[2])/2), ((bx[1]+bx[3])/2))
        # possession = player nearest the ball
        poss = {'team': 'unknown', 'third': 'unknown', 'side': 'unknown',
                'player_shirt_number': None, 'player_role_guess': 'unknown',
                'under_pressure': 'unknown', 'confidence': 'low'}
        ball_state = 'unknown'
        if green < 0.2:
            phase = 'stoppage'  # crowd/closeup/replay-ish
        elif ball is None:
            phase = 'open_play'; ball_state = 'off_screen'
        else:
            phase = 'open_play'; ball_state = 'in_play'
            poss['side'] = side_of(ball[0]/Ww)
            if carrier_pool:
                carrier = min(carrier_pool, key=lambda b: math.hypot((b[0]+b[2])/2-ball[0], (b[1]+b[3])/2-ball[1]))
                dist = math.hypot((carrier[0]+carrier[2])/2-ball[0], (carrier[1]+carrier[3])/2-ball[1])
                if dist < 90:  # close enough to "have" the ball
                    poss['team'] = kit_team(img, carrier)
                    poss['player_role_guess'] = 'keeper' if carrier in keepers else 'unknown'
                    num, ocr_conf, ocr_reg = read_number(reader, img, carrier)
                    poss['player_shirt_number'] = num
                    poss['ocr_region'] = ocr_reg
                    poss['confidence'] = 'high' if (num is not None and ocr_conf > 0.6) else ('medium' if poss['team'] != 'unknown' else 'low')
        # pitch homography → objective ball position along the pitch length (0..1)
        ball_frac = None
        pk = pitch_model.predict(img, verbose=False, device=0)[0].keypoints
        if pk is not None and pk.xy is not None and pk.xy.shape[0] and pk.xy.shape[1]:
            kxy = pk.xy[0].cpu().numpy()
            kcf = pk.conf[0].cpu().numpy() if pk.conf is not None else np.ones(len(kxy))
            mk = (kxy[:, 0] > 1) & (kxy[:, 1] > 1) & (kcf > 0.5)
            if mk.sum() >= 4:
                Hm, _ = cv2.findHomography(kxy[mk].astype(np.float32), PITCH_VERTS[mk].astype(np.float32))
                if Hm is not None:
                    if ball is not None:
                        f = cv2.perspectiveTransform(np.array([[[ball[0], ball[1]]]], np.float32), Hm)[0][0][0] / PITCH_LEN
                        if -0.05 <= f <= 1.05: ball_frac = float(min(1.0, max(0.0, f)))
                    for kb in keepers:
                        kf = cv2.perspectiveTransform(np.array([[[(kb[0]+kb[2])/2, (kb[1]+kb[3])/2]]], np.float32), Hm)[0][0][0] / PITCH_LEN
                        if -0.05 <= kf <= 1.05: orient.append((kit_team(img, kb), float(kf)))
        # team counts among outfield players (what the tracker actually sees)
        nh = na = 0
        for b in players:
            tm = kit_team(img, b)
            if tm == 'home': nh += 1
            elif tm == 'away': na += 1
        det = {'phase': phase, 'possession': poss, 'ball_state': ball_state, 'events': [],
               'tracker': {'players': len(players) + len(keepers), 'mainz': nh, 'union': na,
                           'refs': refs, 'ball_side': poss['side'] if ball is not None else None,
                           'ball_frac': ball_frac}}
        out.append({'burst_i': i, 'video_time_s': round((i+1)*SAMPLE_INTERVAL_S, 2),
                    'frames': [fp.name], 'latency_ms': int((time.monotonic()-tt)*1000),
                    'detection': det})
        if k % 30 == 0:
            print(f"  [{k+1}/{len(idxs)}] t={out[-1]['video_time_s']:.1f}s phase={phase} "
                  f"team={poss['team']} #{poss['player_shirt_number']} side={poss['side']}")
    # clip orientation: which pitch end holds Mainz's goal (from home-kit keeper x)
    hk = [f for t, f in orient if t == 'home']
    ak = [f for t, f in orient if t == 'away']
    if hk:
        mainz_goal_at_zero = float(np.median(hk)) < 0.5
    elif ak:
        mainz_goal_at_zero = float(np.median(ak)) >= 0.5   # Union keeper opposite Mainz goal
    else:
        mainz_goal_at_zero = True  # fallback (arbitrary)
    graded = 0
    for r in out:
        d = r['detection']; bf = d['tracker'].get('ball_frac')
        if bf is None:
            continue
        dist = bf if mainz_goal_at_zero else (1 - bf)   # 0 = at Mainz's own goal
        third = 'home_defensive' if dist < 0.34 else ('middle' if dist < 0.66 else 'home_attacking')
        d['possession']['third'] = third
        d['tracker']['ball_third'] = third
        graded += 1
    with OUT.open('w') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    named = sum(1 for r in out if r['detection']['possession']['player_shirt_number'] is not None)
    teamed = sum(1 for r in out if r['detection']['possession']['team'] != 'unknown')
    print(f"\nwrote {OUT} — {len(out)} bursts, {teamed} with a team, {named} with a shirt#, "
          f"{graded} with a homography-grounded third (mainz_goal_at_zero={mainz_goal_at_zero}, "
          f"{len(orient)} keeper orient samples). wall={time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
