#!/usr/bin/env python3
"""Mux live-captured EN + FR audio tracks with the source video, emit tagged
JSONLs, and publish the synced MP4s to the nginx dir.

Generalised version of publish_live.py (which was hardcoded to v13_live).

Usage:
    python publish_prefix.py <out_prefix>      # e.g. v20_live
"""
import json, subprocess, sys
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
SOURCE = Path('/tmp/v2v_compare/slice_5min.mp4')
PUBLISH_DIR = Path('/var/www/html/experiments/ai_commentator')

prefix = sys.argv[1] if len(sys.argv) > 1 else 'v20_live'


def run(cmd, check=True):
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True)
    if check and r.returncode != 0:
        print(r.stderr.decode(errors='replace')[:400]); sys.exit(1)
    return r


for lang in ('en', 'fr'):
    wav = BASE / f'ai_commentary_{prefix}_{lang}_track.wav'
    if not wav.exists():
        print(f"skip {lang}: {wav} missing"); continue
    out = BASE / f'{prefix}_{lang}_synced.mp4'
    run(['ffmpeg', '-y', '-i', str(SOURCE), '-i', str(wav),
         '-map', '0:v:0', '-map', '1:a:0',
         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '96k', '-shortest',
         str(out), '-loglevel', 'error'])
    pub = PUBLISH_DIR / f'{prefix}_{lang}_synced.mp4'
    # PUBLISH_DIR is owned by the current user, so a plain copy works.
    subprocess.run(['cp', str(out), str(pub)], check=True)
    print(f"  published {pub}")

# emit tagged jsonls for the results page: EN keeps text, FR swaps in the fr field
lines = [json.loads(l) for l in open(BASE / f'commentary_{prefix}.jsonl')]
with open(BASE / f'commentary_{prefix}_en_tagged.jsonl', 'w') as f:
    for r in lines:
        f.write(json.dumps({**r, 'text': r['text']}, ensure_ascii=False) + '\n')
with open(BASE / f'commentary_{prefix}_fr_tagged.jsonl', 'w') as f:
    for r in lines:
        f.write(json.dumps({**r, 'text': r.get('fr', '')}, ensure_ascii=False) + '\n')
print(f"tagged jsonls written; {len(lines)} lines")
