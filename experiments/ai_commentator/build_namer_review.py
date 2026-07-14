#!/usr/bin/env python3
"""Human-review page for the naming run (namer.jsonl).

For each burst it shows: the frame, the model's named ball-carrier (+shirt+
confidence), named pass target, other named players, and the line. A reviewer
marks each as correct / wrong / unsure (persisted in localStorage), sees a live
accuracy tally, and can export their verdicts as JSON. An embedded video seeks
to each moment so passes can be checked in motion.

Usage:
  python build_namer_review.py
"""
import json, shutil, html
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
PUB = Path('/var/www/html/experiments/ai_commentator')
FRAMES = BASE / 'frames'
recs = [json.loads(l) for l in open(BASE / 'namer.jsonl') if l.strip()]

# publish the frames referenced
fdir = PUB / 'namer_frames'; fdir.mkdir(parents=True, exist_ok=True)
for r in recs:
    src = FRAMES / r['newest_frame']
    if src.exists():
        shutil.copy2(src, fdir / r['newest_frame'])

def conf_badge(c):
    c = (c or 'low').lower()
    col = {'high': '#16a34a', 'medium': '#d97706', 'low': '#6b7280'}.get(c, '#6b7280')
    return f"<span class='conf' style='background:{col}'>{c}</span>"

cards = []
for i, r in enumerate(recs):
    hb = r.get('has_ball') or {}
    pt = r.get('pass_to') or {}
    t = r['video_time_s']
    hb_name = hb.get('name')
    names_html = ""
    if hb_name:
        shirt = f"#{hb.get('shirt')}" if hb.get('shirt') else ""
        names_html += f"<div class='nm'><b>has ball:</b> {html.escape(str(hb_name))} {shirt} {conf_badge(hb.get('conf'))}</div>"
    else:
        names_html += "<div class='nm dim'><b>has ball:</b> (unsure)</div>"
    if pt.get('name'):
        names_html += f"<div class='nm'><b>pass to:</b> {html.escape(str(pt.get('name')))} {conf_badge(pt.get('conf'))}</div>"
    others = r.get('others_named') or []
    if others:
        names_html += f"<div class='nm dim'><b>also:</b> {html.escape(', '.join(map(str, others)))}</div>"
    line = html.escape(r.get('line') or '')
    cards.append(f"""
    <div class='card' data-i='{i}' data-t='{t}'>
      <img loading='lazy' src='./namer_frames/{r['newest_frame']}'>
      <div class='body'>
        <div class='t'>{t:.1f}s</div>
        {names_html}
        <div class='line'>“{line}”</div>
        <div class='mark' data-i='{i}'>
          <button class='ok'>✓ correct</button>
          <button class='no'>✗ wrong</button>
          <button class='uns'>? unsure</button>
        </div>
      </div>
    </div>""")

doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Player naming — human review</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:16px}}
h1{{font-size:20px;margin-bottom:4px}} .sub{{color:#888;font-size:13px;margin-bottom:12px}}
.top{{position:sticky;top:0;background:#0a0a0a;padding-bottom:10px;z-index:10;border-bottom:1px solid #222}}
video{{width:100%;max-width:640px;display:block;border-radius:6px;background:#000}}
.tally{{font-size:14px;margin:8px 0}} .tally b{{color:#fbbf24}}
button.exp{{background:#1f2937;color:#a5b4fc;border:1px solid #374151;border-radius:6px;padding:6px 12px;cursor:pointer;font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-top:12px}}
.card{{background:#111;border:1px solid #222;border-radius:8px;overflow:hidden}}
.card.done-ok{{border-color:#166534}} .card.done-no{{border-color:#7f1d1d}} .card.done-uns{{border-color:#78350f}}
.card img{{width:100%;display:block;cursor:pointer}}
.body{{padding:8px 10px}}
.t{{font-family:ui-monospace,monospace;color:#60a5fa;font-size:12px;margin-bottom:4px}}
.nm{{font-size:13px;margin:2px 0}} .nm.dim{{color:#9ca3af}}
.conf{{font-size:10px;color:#fff;padding:1px 6px;border-radius:8px;margin-left:4px}}
.line{{font-style:italic;color:#cbd5e1;font-size:12px;margin:6px 0}}
.mark button{{border:1px solid #333;background:#1a1a1a;color:#ccc;border-radius:5px;padding:4px 8px;font-size:12px;cursor:pointer;margin-right:4px}}
.mark button.sel-ok{{background:#166534;color:#fff}} .mark button.sel-no{{background:#7f1d1d;color:#fff}} .mark button.sel-uns{{background:#78350f;color:#fff}}
</style></head><body>
<div class='top'>
  <h1>Player naming — human review</h1>
  <p class='sub'>For each moment the vision model named a ball-carrier / pass target. Click a frame to seek the video and verify (esp. passes). Mark each ✓/✗/?. Verdicts save in your browser — click Export when done.</p>
  <video id='vid' controls preload='metadata' src='/experiments/ai_commentator/original_with_human_commentary.mp4'></video>
  <div class='tally' id='tally'></div>
  <button class='exp' onclick='exportJSON()'>Export verdicts (JSON)</button>
</div>
<div class='grid'>{''.join(cards)}</div>
<script>
const KEY='namer_review_v1';
let marks=JSON.parse(localStorage.getItem(KEY)||'{{}}');
const vid=document.getElementById('vid');
function tally(){{
  let ok=0,no=0,uns=0;
  for(const k in marks){{if(marks[k]=='ok')ok++;else if(marks[k]=='no')no++;else if(marks[k]=='uns')uns++;}}
  const graded=ok+no; const acc=graded?Math.round(100*ok/graded):0;
  document.getElementById('tally').innerHTML=
    `reviewed <b>${{ok+no+uns}}</b> / {len(recs)} &nbsp;|&nbsp; correct <b>${{ok}}</b> &nbsp; wrong <b>${{no}}</b> &nbsp; unsure <b>${{uns}}</b> &nbsp;|&nbsp; naming accuracy (of graded) <b>${{acc}}%</b>`;
}}
function apply(i){{
  const card=document.querySelector(`.card[data-i='${{i}}']`); if(!card)return;
  const m=marks[i]; card.classList.remove('done-ok','done-no','done-uns');
  const btns=card.querySelectorAll('.mark button'); btns.forEach(b=>b.classList.remove('sel-ok','sel-no','sel-uns'));
  if(m){{card.classList.add('done-'+m); const map={{ok:0,no:1,uns:2}}; btns[map[m]].classList.add('sel-'+m);}}
}}
document.querySelectorAll('.card').forEach(card=>{{
  const i=card.dataset.i, t=parseFloat(card.dataset.t);
  card.querySelector('img').onclick=()=>{{vid.currentTime=Math.max(0,t-1.2);vid.play();}};
  const [b1,b2,b3]=card.querySelectorAll('.mark button');
  b1.onclick=()=>{{marks[i]='ok';save(i);}}; b2.onclick=()=>{{marks[i]='no';save(i);}}; b3.onclick=()=>{{marks[i]='uns';save(i);}};
  apply(i);
}});
function save(i){{localStorage.setItem(KEY,JSON.stringify(marks));apply(i);tally();}}
function exportJSON(){{
  const data=JSON.stringify(marks,null,2);
  const b=new Blob([data],{{type:'application/json'}});const u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download='namer_verdicts.json';a.click();
}}
tally();
</script></body></html>"""

out = BASE / 'namer_review.html'
out.write_text(doc)
shutil.copy2(out, PUB / 'namer_review.html')
print(f"wrote {out} and published to {PUB/'namer_review.html'} ({len(recs)} cards)")
