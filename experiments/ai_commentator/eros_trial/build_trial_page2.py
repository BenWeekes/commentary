#!/usr/bin/env python3
"""Tabbed multi-language Model E trial review page.
Usage: build_trial_page2.py <trial_id> <work_dir> <pkg.json> <www_dir>"""
import json, html, pathlib, sys
tid, work, pkgf, www = sys.argv[1:5]
work=pathlib.Path(work)
langs={}
for f in sorted(work.glob('subs_*.jsonl')):
    code=f.stem.replace('subs_','').replace('_','-')
    langs[code]={json.loads(x)['sequence']:json.loads(x) for x in open(f)}
order=[c for c in ('en','fr','pt-BR','es','tr','zh-CN') if c in langs]+[c for c in langs if c not in ('en','fr','pt-BR','es','tr','zh-CN')]
voiced=[c for c in order if (pathlib.Path(www)/f"modelE_{c.replace('-','_')}.mp4").exists()]
base=langs.get('en') or langs[order[0]]
allseq=sorted(set().union(*[set(s) for s in langs.values()]))
meta={}
for q in allseq:
    src=next(s[q] for s in langs.values() if q in s)
    meta[q]={'t':round(src['source_pts_ms']/1000,1),'p':src['priority']}
place={p['i']:p for p in json.load(open(work/'placement.json'))} if (work/'placement.json').exists() else {}
pkg=json.load(open(pkgf))
lats=sorted(l['latency_ms'] for l in base.values())
def pct(p): return lats[min(len(lats)-1,int(len(lats)*p))] if lats else '—'
def mmss(t): return f"{int(t//60)}:{int(t%60):02d}"
gapinfo=' · '.join(f"{c}:{len(allseq)-len(s)} gaps" for c,s in ((c,langs[c]) for c in order))
rows=''
for q in allseq:
    m=meta[q]
    rows+=(f"<tr data-t={m['t']} data-q={q}><td><a href='#' onclick=\"v.currentTime={m['t']};return false\">{mmss(m['t'])}</a></td>"
           f"<td class=p{m['p']}>p{m['p']}</td><td class=tx id=c{q}></td>"
           f"<td class=fb data-q={q}>💬</td></tr>\n")
LT={c:{q:langs[c][q]['text'] for q in langs[c]} for c in order}
page=f"""<meta charset=utf-8><title>Model E trial {tid} — multi-language review</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13.5px system-ui;margin:16px;padding-bottom:70px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #262626;padding:5px 8px;vertical-align:top}}
th{{background:#161616;position:sticky;top:44px}}a{{color:#7dd3fc}}video{{width:720px;max-width:100%;display:block;margin:8px 0}}
.p0{{color:#ff8d8d}}.p1{{color:#ffc96f}}.p2{{color:#9ecbff}}.p3{{color:#8a8a8a}}
tr.now td{{background:#12222e}}.fb{{cursor:pointer;text-align:center;opacity:.45}}.fb.has{{opacity:1}}
details{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin:10px 0}}
pre{{max-height:340px;overflow:auto;font-size:11.5px;color:#9fb6c9}}
#tabs{{position:sticky;top:0;background:#0a0a0a;padding:8px 0;z-index:5}}
.tab{{display:inline-block;border:1px solid #334155;border-radius:6px;padding:4px 14px;margin-right:6px;cursor:pointer;color:#94a3b8}}
.tab.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px 14px;display:flex;gap:10px;align-items:center}}
#bar input{{background:#0a0f18;border:1px solid #24405e;color:#ddd;padding:5px 8px;border-radius:4px}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:7px 16px;cursor:pointer}}
#box{{position:fixed;right:16px;bottom:64px;width:340px;background:#101826;border:1px solid #1e3a5f;border-radius:8px;padding:10px;display:none}}
#box textarea{{width:100%;height:60px;background:#0a0f18;border:1px solid #24405e;color:#ddd;border-radius:4px}}
.tag{{display:inline-block;border:1px solid #334155;border-radius:9px;padding:1px 8px;margin:2px;cursor:pointer;font-size:11px;color:#94a3b8}}
.tag.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
#st{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:8px}}</style>
<h2>Model E trial <b>{tid}</b> — multi-language (tab switches text AND voice)</h2>
<div id=st>{len(allseq)} lines · latency p50 {pct(.5)} / p95 {pct(.95)} ms · {gapinfo} · a missing row under a tab = that language's translation failed (by design, not an error)</div>
<div id=tabs>{''.join(f"<span class=tab data-l='{c}'>{c}</span>" for c in order)}</div>
<video id=v src="modelE_en.mp4" controls preload=metadata></video>
<details><summary><b>Pre-match data sent to Model E</b></summary><pre>{html.escape(json.dumps(pkg,indent=1))}</pre></details>
<table><tr><th style=width:52px>t</th><th style=width:34px>pri</th><th>Model E commentary <span id=curlang>(en)</span></th><th style=width:34px></th></tr>
{rows}</table>
<div id=box><div id=bt style="margin-bottom:6px;color:#9fb6c9"></div><textarea id=bc placeholder="comment…"></textarea>
<div id=tags>{''.join(f"<span class=tag>{t}</span>" for t in ('wrong fact','repetition','language','naming','timing','👍 good'))}</div>
<div style="margin-top:6px"><button onclick=saveC()>Save</button> <button onclick="box.style.display='none'">Close</button></div></div>
<div id=bar><span>Reviewer:</span><input id=who placeholder=name><span id=cnt>0 unsent</span>
<button onclick=submitAll()>Submit feedback</button><span id=msg></span></div>
<script>
const TID={json.dumps(tid)}, LT={json.dumps(LT)}, META={json.dumps(meta)}, VOICED={json.dumps(voiced)};
const v=document.getElementById('v'), box=document.getElementById('box');
let LANG='en', pend={{}}, cur=-1, noFollow=0;
who.value=localStorage.getItem('reviewer')||''; who.onchange=()=>localStorage.setItem('reviewer',who.value);
function render(){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.l===LANG));
  document.getElementById('curlang').textContent='('+LANG+')';
  const vf='modelE_'+LANG.replace('-','_')+'.mp4';
  if(VOICED.includes(LANG) && !v.src.endsWith(vf)){{const t=v.currentTime,play=!v.paused;v.src=vf;v.currentTime=t;if(play)v.play();}}
  for(const q in META){{
    const c=document.getElementById('c'+q);
    const txt=(LT[LANG]||{{}})[q];
    c.textContent = txt===undefined ? '— (not delivered in '+LANG+')' : txt;
    c.style.opacity = txt===undefined ? .35 : 1;
  }}
}}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{{LANG=t.dataset.l;render();}});
render();
addEventListener('wheel',()=>noFollow=Date.now()+6000); addEventListener('touchmove',()=>noFollow=Date.now()+6000);
v.addEventListener('timeupdate',()=>{{const t=v.currentTime;let best=null;
document.querySelectorAll('tr[data-t]').forEach(r=>{{if(parseFloat(r.dataset.t)<=t)best=r.dataset.q;}});
if(best!==cur){{cur=best;document.querySelectorAll('tr.now').forEach(r=>r.classList.remove('now'));
const r=document.querySelector(`tr[data-q="${{best}}"]`);
if(r){{r.classList.add('now');if(Date.now()>noFollow)r.scrollIntoView({{block:'center',behavior:'smooth'}});}}}}}});
document.querySelectorAll('.fb').forEach(c=>c.onclick=()=>{{const q=c.dataset.q;box.dataset.q=q;
bt.textContent=`${{META[q].t}}s [${{LANG}}] — ${{((LT[LANG]||{{}})[q]||'').slice(0,60)}}`;
const k=LANG+':'+q;
bc.value=(pend[k]||{{}}).comment||'';
document.querySelectorAll('#tags .tag').forEach(el=>el.classList.toggle('on',((pend[k]||{{}}).tags||[]).includes(el.textContent)));
box.style.display='block';}});
document.querySelectorAll('#tags .tag').forEach(el=>el.onclick=()=>el.classList.toggle('on'));
function saveC(){{const q=box.dataset.q, k=LANG+':'+q;
const tags=[...document.querySelectorAll('#tags .tag.on')].map(e=>e.textContent);
if(bc.value.trim()||tags.length){{pend[k]={{comment:bc.value.trim(),tags:tags,lang:LANG,q:q}};
document.querySelector(`.fb[data-q="${{q}}"]`).classList.add('has');}}else delete pend[k];
cnt.textContent=Object.keys(pend).length+' unsent';box.style.display='none';}}
function submitAll(){{const w=who.value.trim();if(!w){{msg.textContent='enter reviewer name';return;}}
const items=Object.values(pend).map(c=>({{t:META[c.q].t,col:3,column:'Model E '+c.lang,profile:'modelE',
clip:TID,cell_text:(LT[c.lang]||{{}})[c.q]||'',tags:c.tags,comment:c.comment}}));
if(!items.length){{msg.textContent='nothing to send';return;}}
fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:w,version:'modelE'+TID,items:items}})}})
.then(r=>r.json()).then(j=>{{msg.textContent=j.ok?'sent ✓':'error: '+(j.error||'');if(j.ok){{pend={{}};cnt.textContent='0 unsent';
document.querySelectorAll('.fb.has').forEach(e=>e.classList.remove('has'));}}}})
.catch(()=>msg.textContent='network error');}}
</script>"""
w=pathlib.Path(www); w.mkdir(parents=True, exist_ok=True)
(w/'index.html').write_text(page)
print('page ->', w/'index.html', '| langs:', order, '| lines:', len(allseq))
