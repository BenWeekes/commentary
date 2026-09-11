#!/usr/bin/env python3
import json, html, pathlib
HERE=pathlib.Path(__file__).parent
sim=json.load(open(HERE/'sim.json'))
lines=sim['lines']; ev={e['i']:e for e in sim['events']}
def mmss(t): return f"{int(t//60)}:{int(t%60):02d}"
rows=''
for i,l in enumerate(lines):
    e=ev.get(i,{})
    if 'ready' in e: st=f"<span class=ok>✓ signed at +{e['ready']-l['t']:.0f}s (gen {e['gen']}s)</span>"
    elif e.get('dropped'): st="<span class=drop>✖ dropped (live backlog)</span>"
    elif e.get('failed'): st="<span class=drop>generation failed</span>"
    else: st=""
    rows+=(f"<tr data-t={l['t']:.1f}><td><a href='#' onclick=\"v.currentTime={l['t']:.1f};return false\">{mmss(l['t'])}</a></td>"
           f"<td>{html.escape(l['text'])} {st}</td><td class=fb data-i={i}>💬</td></tr>\n")
signed=sum(1 for e in sim['events'] if 'ready' in e); dropped=sum(1 for e in sim['events'] if e.get('dropped'))
gens=sorted(e['gen'] for e in sim['events'] if 'gen' in e)
page=f"""<meta charset=utf-8><title>Horse racing — live ASL signer trial</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13.5px system-ui;margin:16px;padding-bottom:70px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #262626;padding:5px 8px;vertical-align:top}}
th{{background:#161616;position:sticky;top:0}}a{{color:#7dd3fc}}video{{width:720px;max-width:100%;display:block;margin:8px 0}}
.ok{{color:#6ee7a8;font-size:11px}}.drop{{color:#f87171;font-size:11px}}tr.now td{{background:#12222e}}
.fb{{cursor:pointer;text-align:center;opacity:.45}}.fb.has{{opacity:1}}
#st{{background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:8px}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px 14px;display:flex;gap:10px;align-items:center}}
#bar input{{background:#0a0f18;border:1px solid #24405e;color:#ddd;padding:5px 8px;border-radius:4px}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:7px 16px;cursor:pointer}}
#box{{position:fixed;right:16px;bottom:64px;width:340px;background:#101826;border:1px solid #1e3a5f;border-radius:8px;padding:10px;display:none}}
#box textarea{{width:100%;height:60px;background:#0a0f18;border:1px solid #24405e;color:#ddd;border-radius:4px}}</style>
<h2>Horse racing — LIVE sign-language trial (transparent signer, honest latency)</h2>
<div id=st>{len(lines)} commentary lines (STT) · signed live {signed} · dropped by backlog {dropped}
· Signapse generation p50 {gens[len(gens)//2] if gens else '?'}s · signer appears at its TRUE ready time
(STT finalize + generation + worker queue, 3 workers)</div>
<div style="margin:6px 0"><span class=vtab data-s="horse_asl.mp4" style="border:1px solid #334155;border-radius:6px;padding:4px 14px;margin-right:6px;cursor:pointer;background:#1e3a5f;color:#dbeafe">live latency</span><span class=vtab data-s="horse_asl_sync.mp4" style="border:1px solid #334155;border-radius:6px;padding:4px 14px;cursor:pointer;color:#94a3b8">synced</span></div>
<video id=v src="horse_asl.mp4" controls preload=metadata></video>
<table><tr><th style=width:52px>t</th><th>Commentary (Soniox STT) + signing fate</th><th style=width:34px></th></tr>
{rows}</table>
<div id=box><div id=bt style="margin-bottom:6px;color:#9fb6c9"></div><textarea id=bc placeholder="comment…"></textarea>
<div style="margin-top:6px"><button onclick=saveC()>Save</button> <button onclick="box.style.display='none'">Close</button></div></div>
<div id=bar><span>Reviewer:</span><input id=who placeholder=name><span id=cnt>0 unsent</span>
<button onclick=submitAll()>Submit feedback</button><span id=msg></span></div>
<script>
const LINES={json.dumps([{'t':round(l['t'],1),'text':l['text']} for l in lines])};
const v=document.getElementById('v'); let pend={{}},cur=-1,noFollow=0;
who.value=localStorage.getItem('reviewer')||''; who.onchange=()=>localStorage.setItem('reviewer',who.value);
addEventListener('wheel',()=>noFollow=Date.now()+6000);
document.querySelectorAll('.vtab').forEach(t=>t.onclick=()=>{{const tt=v.currentTime,pl=!v.paused;v.src=t.dataset.s;v.currentTime=tt;if(pl)v.play();
document.querySelectorAll('.vtab').forEach(x=>{{x.style.background=x===t?'#1e3a5f':'';x.style.color=x===t?'#dbeafe':'#94a3b8';}});}});
v.addEventListener('timeupdate',()=>{{const t=v.currentTime;let best=null;
document.querySelectorAll('tr[data-t]').forEach(r=>{{if(parseFloat(r.dataset.t)<=t)best=r;}});
if(best&&best!==cur){{cur&&cur.classList&&cur.classList.remove('now');cur=best;best.classList.add('now');
}}}});
document.querySelectorAll('.fb').forEach(c=>c.onclick=()=>{{const i=+c.dataset.i;box.dataset.i=i;
bt.textContent=LINES[i].t+'s — '+LINES[i].text.slice(0,60);bc.value=(pend[i]||{{}}).comment||'';box.style.display='block';}});
function saveC(){{const i=+box.dataset.i;
if(bc.value.trim()){{pend[i]={{comment:bc.value.trim()}};document.querySelector(`.fb[data-i="${{i}}"]`).classList.add('has');}}
else delete pend[i];cnt.textContent=Object.keys(pend).length+' unsent';box.style.display='none';}}
function submitAll(){{const w=who.value.trim();if(!w){{msg.textContent='enter reviewer name';return;}}
const items=Object.entries(pend).map(([i,c])=>({{t:LINES[i].t,col:3,column:'ASL',profile:'horse',
clip:'horse1',cell_text:LINES[i].text,tags:[],comment:c.comment}}));
if(!items.length){{msg.textContent='nothing to send';return;}}
fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:w,version:'asrhorse1',items:items}})}})
.then(r=>r.json()).then(j=>{{msg.textContent=j.ok?'sent ✓':'error';if(j.ok){{pend={{}};cnt.textContent='0 unsent';}}}});}}
</script>"""
out=pathlib.Path('/var/www/html/experiments/ai_commentator/horse_asl/index.html')
out.write_text(page); print('page ->',out)
