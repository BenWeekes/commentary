#!/usr/bin/env python3
"""Eros trial review page: voiced synced video + pre-match data + line table with feedback.
Usage: build_trial_page.py <trial_id> <subs_en.jsonl> <placement.json> <pkg.json> <www_dir>"""
import json, html, pathlib, sys
tid, subsf, placef, pkgf, www = sys.argv[1:6]
subs=[json.loads(x) for x in open(subsf)]; subs.sort(key=lambda l:l['source_pts_ms'])
place={p['i']:p for p in json.load(open(placef))}
pkg=json.load(open(pkgf))
lats=sorted(l.get('latency_ms',0) for l in subs)
def pct(p): return lats[min(len(lats)-1,int(len(lats)*p))] if lats else '—'
def mmss(t): return f"{int(t//60)}:{int(t%60):02d}"
rows=''
for i,l in enumerate(subs):
    t=l['source_pts_ms']/1000; pl=place.get(i)
    spoke=f" → spoken {mmss(pl['t'])}" if pl and pl['t']-t>0.3 else ''
    rows+=(f"<tr data-t={t:.1f} data-i={i}><td><a href='#' onclick=\"v.currentTime={t:.1f};return false\">{mmss(t)}</a></td>"
      f"<td class=p{l['priority']}>p{l['priority']}</td><td class=tx>{html.escape(l['text'])}"
      f"<span class=m> {l.get('latency_ms','?')}ms{spoke}</span></td>"
      f"<td class=fb data-i={i}>💬</td></tr>\n")
page=f"""<meta charset=utf-8><title>Eros trial {tid} — review</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13.5px system-ui;margin:16px;padding-bottom:70px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #262626;padding:5px 8px;vertical-align:top}}
th{{background:#161616;position:sticky;top:0}}a{{color:#7dd3fc}}video{{width:720px;max-width:100%;display:block;margin:8px 0}}
.m{{color:#5b6b7a;font-size:11px}}.p0{{color:#ff8d8d}}.p1{{color:#ffc96f}}.p2{{color:#9ecbff}}.p3{{color:#8a8a8a}}
tr.now td{{background:#12222e}}.fb{{cursor:pointer;text-align:center;opacity:.45}}.fb.has{{opacity:1}}
details{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin:10px 0}}
pre{{max-height:340px;overflow:auto;font-size:11.5px;color:#9fb6c9}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px 14px;display:flex;gap:10px;align-items:center}}
#bar input{{background:#0a0f18;border:1px solid #24405e;color:#ddd;padding:5px 8px;border-radius:4px}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:7px 16px;cursor:pointer}}
#box{{position:fixed;right:16px;bottom:64px;width:340px;background:#101826;border:1px solid #1e3a5f;border-radius:8px;padding:10px;display:none}}
#box textarea{{width:100%;height:60px;background:#0a0f18;border:1px solid #24405e;color:#ddd;border-radius:4px}}
.tag{{display:inline-block;border:1px solid #334155;border-radius:9px;padding:1px 8px;margin:2px;cursor:pointer;font-size:11px;color:#94a3b8}}
.tag.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
#st{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:8px}}</style>
<h2>Eros (nextmoment.ai) trial <b>{tid}</b> — English, voiced with our ElevenLabs commentator</h2>
<div id=st>{len(subs)} lines · latency p50 {pct(.5)} / p95 {pct(.95)} ms · priorities:
{'/'.join(str(sum(1 for s in subs if s['priority']==p)) for p in (0,1,2,3))} (p0/p1/p2/p3) ·
lines placed at their <code>source_pts_ms</code>; "spoken" notes show overlap shifts</div>
<video id=v src="eros_en.mp4" controls preload=metadata></video>
<details><summary><b>Pre-match data sent to Eros</b> (match_package)</summary><pre>{html.escape(json.dumps(pkg,indent=1))}</pre></details>
<p>Click a time to seek · click 💬 to comment a line · playing row highlights (scroll pauses follow 6s) · Submit sends to the shared feedback store.</p>
<table><tr><th style=width:52px>t</th><th style=width:34px>pri</th><th>Eros commentary (EN)</th><th style=width:34px></th></tr>
{rows}</table>
<div id=box><div id=bt style="margin-bottom:6px;color:#9fb6c9"></div><textarea id=bc placeholder="comment…"></textarea>
<div id=tags>{''.join(f"<span class=tag>{t}</span>" for t in ('wrong fact','repetition','language','naming','timing','👍 good'))}</div>
<div style="margin-top:6px"><button onclick=saveC()>Save</button> <button onclick="box.style.display='none'">Close</button></div></div>
<div id=bar><span>Reviewer:</span><input id=who placeholder=name><span id=cnt>0 unsent</span>
<button onclick=submitAll()>Submit feedback</button><span id=msg></span></div>
<script>
const TID={json.dumps(tid)}, LINES={json.dumps([{'t':round(l['source_pts_ms']/1000,1),'text':l['text']} for l in subs])};
const v=document.getElementById('v'), box=document.getElementById('box');
let pend={{}}, cur=-1, noFollow=0;
who.value=localStorage.getItem('reviewer')||''; who.onchange=()=>localStorage.setItem('reviewer',who.value);
addEventListener('wheel',()=>noFollow=Date.now()+6000); addEventListener('touchmove',()=>noFollow=Date.now()+6000);
v.addEventListener('timeupdate',()=>{{const t=v.currentTime;let best=-1;
document.querySelectorAll('tr[data-t]').forEach(r=>{{if(parseFloat(r.dataset.t)<=t)best=+r.dataset.i;}});
if(best!==cur){{cur=best;document.querySelectorAll('tr.now').forEach(r=>r.classList.remove('now'));
const r=document.querySelector(`tr[data-i="${{best}}"]`);
if(r){{r.classList.add('now');if(Date.now()>noFollow)r.scrollIntoView({{block:'center',behavior:'smooth'}});}}}}}});
document.querySelectorAll('.fb').forEach(c=>c.onclick=()=>{{const i=+c.dataset.i;box.dataset.i=i;
bt.textContent=`${{LINES[i].t}}s — ${{LINES[i].text.slice(0,60)}}`;
bc.value=(pend[i]||{{}}).comment||'';
document.querySelectorAll('#tags .tag').forEach(el=>el.classList.toggle('on',((pend[i]||{{}}).tags||[]).includes(el.textContent)));
box.style.display='block';}});
document.querySelectorAll('#tags .tag').forEach(el=>el.onclick=()=>el.classList.toggle('on'));
function saveC(){{const i=+box.dataset.i;
const tags=[...document.querySelectorAll('#tags .tag.on')].map(e=>e.textContent);
if(bc.value.trim()||tags.length){{pend[i]={{comment:bc.value.trim(),tags:tags}};
document.querySelector(`.fb[data-i="${{i}}"]`).classList.add('has');}}else delete pend[i];
cnt.textContent=Object.keys(pend).length+' unsent';box.style.display='none';}}
function submitAll(){{const w=who.value.trim();if(!w){{msg.textContent='enter reviewer name';return;}}
const items=Object.entries(pend).map(([i,c])=>({{t:LINES[i].t,col:3,column:'Eros EN',profile:'eros',
clip:TID,cell_text:LINES[i].text,tags:c.tags,comment:c.comment}}));
if(!items.length){{msg.textContent='nothing to send';return;}}
fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:w,version:'eros'+TID,items:items}})}})
.then(r=>r.json()).then(j=>{{msg.textContent=j.ok?'sent ✓':'error: '+(j.error||'');if(j.ok){{pend={{}};cnt.textContent='0 unsent';
document.querySelectorAll('.fb.has').forEach(e=>e.classList.remove('has'));}}}})
.catch(()=>msg.textContent='network error');}}
</script>"""
w=pathlib.Path(www); w.mkdir(parents=True, exist_ok=True)
(w/'index.html').write_text(page)
print('page ->', w/'index.html')
