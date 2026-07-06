#!/usr/bin/env python3
"""Mux the live-captured EN + FR audio tracks with the source video and publish."""
import json, subprocess, sys
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
SOURCE = Path('/tmp/v2v_compare/slice_5min.mp4')
PUBLISH_DIR = Path('/var/www/html/experiments/ai_commentator')

def run(cmd, check=True):
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True)
    if check and r.returncode != 0:
        print(r.stderr.decode(errors='replace')[:400]); sys.exit(1)
    return r

for lang in ('en', 'fr'):
    wav = BASE / f'ai_commentary_v13_live_{lang}_track.wav'
    if not wav.exists():
        print(f"skip {lang}: {wav} missing"); continue
    out = BASE / f'v13_live_{lang}_synced.mp4'
    run(['ffmpeg', '-y', '-i', str(SOURCE), '-i', str(wav),
         '-map', '0:v:0', '-map', '1:a:0',
         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '96k', '-shortest',
         str(out), '-loglevel', 'error'])
    pub = PUBLISH_DIR / f'v13_live_{lang}_synced.mp4'
    subprocess.run(['sudo', 'cp', str(out), str(pub)], check=True)
    print(f"  published {pub}")

# emit a tagged jsonl for the results page
lines = [json.loads(l) for l in open(BASE/'commentary_v13_live.jsonl')]
with open(BASE/'commentary_v13_live_en_tagged.jsonl', 'w') as f:
    for r in lines:
        f.write(json.dumps({**r, 'text': r['text']}, ensure_ascii=False) + '\n')
with open(BASE/'commentary_v13_live_fr_tagged.jsonl', 'w') as f:
    for r in lines:
        f.write(json.dumps({**r, 'text': r.get('fr','')}, ensure_ascii=False) + '\n')
print(f"tagged jsonls written; {len(lines)} lines")
