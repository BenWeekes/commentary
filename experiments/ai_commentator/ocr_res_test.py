#!/usr/bin/env python3
"""Jersey-number OCR read-rate at a given frame resolution.

Detects every player/keeper in sampled frames and runs the tracker's
(shirt + shorts) OCR on each, tallying: players seen, numbers read, read-rate,
which region won (shirt vs shorts), and mean OCR confidence. Run it once per
resolution to see whether 1080p reads more numbers than 720p.

Usage: .venv-track/bin/python ocr_res_test.py <frames_dir> [stride=8] [limit=0]
"""
import sys, json, collections
from pathlib import Path
import cv2

sys.path.insert(0, '/home/ubuntu/commentary/experiments/ai_commentator')
from run_tracker_detector import read_number, MODELS
from ultralytics import YOLO
import easyocr


def run(frames_dir, stride=8, limit=0):
    player_model = YOLO(str(MODELS / 'football-player-detection.pt'))
    reader = easyocr.Reader(['en'], gpu=True)
    frames = sorted(Path(frames_dir).glob('f_*.jpg'))
    idxs = list(range(0, len(frames), stride))
    if limit:
        idxs = idxs[:limit]
    players = reads = 0
    reg = collections.Counter(); confs = []; box_h = []
    res_wh = None
    for i in idxs:
        img = cv2.imread(str(frames[i]))
        if img is None:
            continue
        if res_wh is None:
            res_wh = [int(img.shape[1]), int(img.shape[0])]
        r = player_model.predict(img, conf=0.35, verbose=False, device=0)[0]
        for box, cls in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.cpu().numpy()):
            if int(cls) not in (1, 2):   # 1=keeper, 2=player (skip ball/ref)
                continue
            players += 1
            box_h.append(float(box[3] - box[1]))
            num, conf, region = read_number(reader, img, box)
            if num is not None:
                reads += 1; reg[region] += 1; confs.append(float(conf))
    return {
        'res': res_wh, 'frames_sampled': len(idxs), 'players': players, 'reads': reads,
        'read_rate': round(reads / players, 3) if players else 0.0,
        'by_region': dict(reg),
        'mean_conf': round(sum(confs) / len(confs), 3) if confs else 0.0,
        'median_player_box_px_h': round(sorted(box_h)[len(box_h) // 2], 1) if box_h else 0.0,
    }


if __name__ == '__main__':
    fd = sys.argv[1]
    stride = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    print(json.dumps(run(fd, stride, limit)))
