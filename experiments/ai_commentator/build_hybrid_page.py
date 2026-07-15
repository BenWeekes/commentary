#!/usr/bin/env python3
"""Hybrid live-commentary page — five columns on ONE common timeline:

  A, B, C  = three live input signals (deliberately UNLABELLED on the page —
             the source mix is proprietary), then the fused outputs:
  Blend EN, Blend FR.

Internally A=live STT phrases, B=vision detector read, C=tracker location/shape,
but the page must not disclose that. Shared rows, holes shown, one scroll box,
scrub-sync + click-seek, audio toggle Original / AI-EN / AI-FR.

Usage: python build_hybrid_page.py
"""
import json, html, shutil
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
ROOT = Path('/var/www/html/experiments/ai_commentator/blend')   # served location
ROOT.mkdir(parents=True, exist_ok=True)
ROW_MERGE_S = 1.2


def load(p):
    p = Path(p)
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


def fmt(s):
    s = float(s); return f"{int(s//60):02d}:{int(s%60):02d}"


VIDS = [('original', 'original_with_human_commentary.mp4'),
        ('blend_en', 'blend_live_en_synced.mp4'),
        ('blend_fr', 'blend_live_fr_synced.mp4')]
have = {}
for name, src in VIDS:
    sp = BASE / src
    if sp.exists():
        shutil.copy2(sp, ROOT / f'{name}.mp4'); have[name] = True

son = load(BASE / 'soniox_live_short.jsonl')   # A
oai = load(BASE / 'oai_col.jsonl')             # B
trk = load(BASE / 'tracker_col.jsonl')         # C
blend = load(BASE / 'commentary_blend_live.jsonl')

items = []   # (t, col_index 0..4, cell_html)
for r in son:
    c = float(r.get('conf', 0))
    items.append((float(r['video_time_s']), 0,
                  f"<span class='cf a{'hi' if c >= 0.9 else 'md'}'>{c:.2f}</span>" + html.escape(r['text'])))
for r in oai:
    cf = r.get('conf', '')
    d = html.escape(r.get('detail') or '')
    items.append((float(r['video_time_s']), 1,
                  f"<span class='cf b{cf}'>{html.escape(str(cf))}</span>" + html.escape(r['text'])
                  + (f"<span class='det'>{d}</span>" if d else '')))
for r in trk:
    d = html.escape(r.get('detail') or '')
    items.append((float(r['video_time_s']), 2,
                  html.escape(r['text']) + (f"<span class='det'>{d}</span>" if d else '')))
for r in blend:
    if r.get('dropped'):
        continue          # missed the live buffer — never heard, never shown
    t = float(r.get('video_time_s', 0))
    items.append((t, 3, html.escape(r.get('text', ''))))     # no source badge — mix is hidden
    fr = r.get('fr') or ''
    if fr:
        items.append((t, 4, html.escape(fr)))

items.sort(key=lambda x: x[0])
rows = []
for t, col, cell in items:
    if not rows or t - rows[-1]['t0'] > ROW_MERGE_S:
        rows.append({'t': t, 't0': t, 'cells': [[], [], [], [], []]})
    rows[-1]['cells'][col].append(cell)

def cell_html(cs):
    return "".join(f"<div class='ln'>{c}</div>" for c in cs) if cs else "<div class='hole'></div>"

body = ""
for row in rows:
    tds = "".join(f"<div class='cell'>{cell_html(row['cells'][i])}</div>" for i in range(5))
    body += (f"<div class='row' data-t='{row['t']:.2f}'><div class='tc'>{fmt(row['t'])}</div>{tds}</div>")

tabs = []
if have.get('original'): tabs.append("<button data-src='./original.mp4'>Original broadcast</button>")
tabs.append("<button data-src='./blend_en.mp4' class='active'>AI · English</button>")
if have.get('blend_fr'): tabs.append("<button data-src='./blend_fr.mp4'>AI · French</button>")

doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>AI live commentary — Mainz vs Union</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:14px}}
h1{{font-size:18px}} .sub{{color:#8a8a8a;font-size:12.5px;margin:3px 0 10px;max-width:1100px}}
.top{{position:sticky;top:0;background:#0a0a0a;z-index:6;padding-bottom:10px;border-bottom:1px solid #222}}
video{{width:100%;max-width:640px;display:block;margin:0 auto 8px;border-radius:6px;background:#000}}
.tabs{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}}
.tabs button{{background:#1a1a1a;color:#aaa;border:1px solid #2a2a2a;padding:7px 15px;font-size:12.5px;border-radius:18px;cursor:pointer;font-weight:600}}
.tabs button.active{{background:#052e16;color:#4ade80;border-color:#166534}}
.wrap{{margin-top:12px;border:1px solid #1f1f1f;border-radius:7px;overflow-x:auto}}
.grid{{min-width:820px}}
.head,.row{{display:grid;grid-template-columns:50px repeat(5,1fr)}}
.head{{position:sticky;top:0;background:#141414;z-index:2;border-bottom:1px solid #262626}}
.head>div{{padding:8px 9px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}}
.head .h0{{color:#666}} .head .hA{{color:#fbbf24}} .head .hB{{color:#a78bfa}} .head .hC{{color:#38bdf8}}
.head .hEN{{color:#34d399}} .head .hFR{{color:#60a5fa}}
.scroll{{max-height:74vh;overflow-y:auto}}
.row{{border-top:1px solid #161616;cursor:pointer}}
.row:hover{{background:#141414}} .row.active{{background:#16233a}}
.row.active .tc{{color:#4ade80}}
.tc{{padding:7px 6px;font-family:ui-monospace,monospace;font-size:10px;color:#5a5a5a;border-right:1px solid #161616}}
.cell{{padding:5px 8px;border-right:1px solid #161616;font-size:12px;line-height:1.42}}
.cell:last-child{{border-right:none}}
.ln{{padding:2px 0}} .ln+.ln{{border-top:1px dashed #1c1c1c;margin-top:2px;padding-top:4px}}
.det{{display:block;color:#7a7a7a;font-size:10.5px;line-height:1.35;margin-top:2px}}
.hole{{height:6px}}
.cf{{display:inline-block;font-family:ui-monospace,monospace;font-size:9px;padding:0 4px;border-radius:7px;margin-right:5px;vertical-align:1px}}
.cf.ahi{{background:#052e16;color:#4ade80}} .cf.amd{{background:#3a2e00;color:#fbbf24}}
.cf.bhigh{{background:#052e16;color:#4ade80}} .cf.bmedium{{background:#3a2e00;color:#fbbf24}}
</style></head><body>
<div class='top'>
  <h1>AI live commentary — Mainz vs Union Berlin</h1>
  <p class='sub'>One commentary track built live over the SRT feed and voiced in English and French.
  <b>STT</b> = short, high-confidence phrases from the broadcast audio. <b>Vision</b> = what the
  system reads from the video frames (possession, events), shown with its own confidence.
  <b>Tracker</b> = objective on-pitch positions and shape. The blend fuses them into one spoken
  line, preferring real phrases and filling the gaps. Holes are where a signal stayed silent.
  Pick an audio track; click any row to seek.</p>
  <video id='v' controls preload='metadata' src='./blend_en.mp4'></video>
  <div class='tabs'>{''.join(tabs)}</div>
</div>
<div class='wrap'><div class='grid'>
  <div class='head'><div class='h0'>time</div><div class='hA'>STT</div><div class='hB'>Vision</div><div class='hC'>Tracker</div><div class='hEN'>Blend · EN</div><div class='hFR'>Blend · FR</div></div>
  <div class='scroll' id='sc'>{body}</div>
</div></div>
<script>
const v=document.getElementById('v'), sc=document.getElementById('sc');
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); const t=v.currentTime,p=!v.paused; v.src=b.dataset.src;
  v.addEventListener('loadedmetadata',()=>{{v.currentTime=t; if(p)v.play();}},{{once:true}});
}}));
const rows=[...document.querySelectorAll('.row')];
rows.forEach(r=>r.addEventListener('click',()=>{{v.currentTime=Math.max(0,parseFloat(r.dataset.t)-0.3);v.play();}}));
let cur=null;
v.addEventListener('timeupdate',()=>{{const t=v.currentTime; let a=null;
  for(const r of rows){{if(parseFloat(r.dataset.t)<=t+0.05)a=r;else break;}}
  if(a!==cur){{if(cur)cur.classList.remove('active'); if(a){{a.classList.add('active');
    const rt=a.getBoundingClientRect(), st=sc.getBoundingClientRect();
    if(rt.top<st.top+40||rt.bottom>st.bottom-20) a.scrollIntoView({{block:'center',behavior:'smooth'}});}}
    cur=a;}}}});
</script></body></html>"""
(ROOT / 'index.html').write_text(doc)
nb = len(blend)
print(f"wrote {ROOT/'index.html'}  rows={len(rows)}  A={len(son)} B={len(oai)} C={len(trk)} blend={nb}")
