#!/usr/bin/env python3
"""Results page for the blended live commentary: Original + AI-English + AI-French
videos (audio-track toggle over one player) and three synced transcript columns.

Usage: python build_blend_page.py
"""
import json, html, shutil
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
ROOT = Path('/var/www/html/experiments/ai_commentator/blend')
ROOT.mkdir(parents=True, exist_ok=True)

def load(p):
    p = Path(p)
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []

def fmt(s):
    s = float(s); return f"{int(s//60):02d}:{int(s%60):02d}"

# publish the three videos
VIDS = [('original', BASE / 'original_with_human_commentary.mp4'),
        ('blend_en', BASE / 'blend_live_en_synced.mp4'),
        ('blend_fr', BASE / 'blend_live_fr_synced.mp4')]
for name, src in VIDS:
    if src.exists(): shutil.copy2(src, ROOT / f'{name}.mp4')

gold = load(BASE / 'gold_sentences.jsonl')   # same gold, sentence-level — matches the AI 'real' phrases
blend = load(BASE / 'commentary_blend_live.jsonl')

def rows(entries, kind):
    out = []
    for e in entries:
        if kind == 'orig':
            t = e.get('video_time_s', e.get('start_s', 0)); body = html.escape(e.get('text', ''))
        elif kind == 'en':
            t = e.get('video_time_s', 0)
            badge = "<span class='b son'>real</span>" if e.get('src') == 'soniox' else "<span class='b gen'>AI</span>"
            body = badge + html.escape(e.get('text', ''))
        else:  # fr
            t = e.get('video_time_s', 0); body = html.escape(e.get('fr', '') or '')
        out.append(f"<div class='row' data-t='{float(t):.2f}'><span class='ts'>{fmt(t)}</span>"
                   f"<span class='c'>{body}</span></div>")
    return ''.join(out) or "<div class='soon'>—</div>"

ns = sum(1 for b in blend if b.get('src') == 'soniox')
doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>Blended commentary — Mainz vs Union</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:16px}}
h1{{font-size:19px}} .sub{{color:#888;font-size:13px;margin:3px 0 12px}}
.top{{position:sticky;top:0;background:#0a0a0a;z-index:5;padding-bottom:10px;border-bottom:1px solid #222}}
video{{width:100%;max-width:760px;display:block;margin:0 auto 10px;border-radius:6px;background:#000}}
.tabs{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}}
.tabs button{{background:#1a1a1a;color:#aaa;border:1px solid #2a2a2a;padding:8px 16px;font-size:13px;border-radius:18px;cursor:pointer;font-weight:600}}
.tabs button.active{{background:#052e16;color:#4ade80;border-color:#166534}}
.cols{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}
.col{{background:#0e0e0e;border:1px solid #1f1f1f;border-radius:6px;overflow:hidden}}
.h{{padding:8px 11px;border-bottom:1px solid #1f1f1f;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}}
.col-orig .h{{color:#fbbf24}} .col-en .h{{color:#34d399}} .col-fr .h{{color:#60a5fa}}
.rows{{max-height:70vh;overflow-y:auto;padding:3px 0}}
.row{{padding:6px 10px;cursor:pointer;border-left:3px solid transparent;font-size:12.5px;line-height:1.4}}
.row:hover{{background:#161616}} .row.active{{background:#16233a;border-left-color:#4ade80}}
.ts{{display:inline-block;min-width:46px;font-family:ui-monospace,monospace;font-size:10px;color:#666;margin-right:6px}}
.b{{font-size:9px;font-weight:700;padding:0 5px;border-radius:8px;margin-right:5px}}
.b.son{{background:#2d2400;color:#fbbf24}} .b.gen{{background:#052e16;color:#4ade80}}
.soon{{padding:24px;text-align:center;color:#555}}
</style></head><body>
<div class='top'>
  <h1>Blended live commentary — Mainz vs Union Berlin</h1>
  <p class='sub'>One track built live over the SRT feed: real short broadcaster phrases (verbatim) + grounded AI lines (player-named from roster, tracker-located) in the gaps. {len(blend)} lines ({ns} real, {len(blend)-ns} AI). Pick an audio track; click any line to seek.</p>
  <video id='v' controls preload='metadata' src='./blend_en.mp4'></video>
  <div class='tabs'>
    <button data-src='./original.mp4'>Original broadcast</button>
    <button data-src='./blend_en.mp4' class='active'>AI · English</button>
    <button data-src='./blend_fr.mp4'>AI · French</button>
  </div>
</div>
<div class='cols'>
  <div class='col col-orig'><div class='h'>Original booth (Soniox)</div><div class='rows'>{rows(gold,'orig')}</div></div>
  <div class='col col-en'><div class='h'>AI English (blend)</div><div class='rows'>{rows(blend,'en')}</div></div>
  <div class='col col-fr'><div class='h'>AI French (blend)</div><div class='rows'>{rows(blend,'fr')}</div></div>
</div>
<script>
const v=document.getElementById('v');
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); const t=v.currentTime, p=!v.paused; v.src=b.dataset.src;
  v.addEventListener('loadedmetadata',()=>{{v.currentTime=t; if(p)v.play();}},{{once:true}});
}}));
document.querySelectorAll('.row').forEach(r=>r.addEventListener('click',()=>{{v.currentTime=Math.max(0,parseFloat(r.dataset.t)-0.3);v.play();}}));
v.addEventListener('timeupdate',()=>{{const t=v.currentTime;
  document.querySelectorAll('.col').forEach(c=>{{let a=null;c.querySelectorAll('.row').forEach(r=>{{if(parseFloat(r.dataset.t)<=t+0.05)a=r;r.classList.remove('active');}});if(a)a.classList.add('active');}});}});
</script></body></html>"""
(ROOT / 'index.html').write_text(doc)
print(f"wrote {ROOT/'index.html'}  ({len(blend)} lines, {ns} real / {len(blend)-ns} AI)")
