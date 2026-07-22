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
import os
VERSION = os.environ.get('PAGE_VERSION', '')          # e.g. 'v2' -> serve at blend_v2/
ROOT = Path('/var/www/html/experiments/ai_commentator/' +
            ('blend' if not VERSION else f'blend_{VERSION}'))
ROOT.mkdir(parents=True, exist_ok=True)
ROW_MERGE_S = 1.2


def load(p):
    p = Path(p)
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


def fmt(s):
    s = float(s); return f"{int(s//60):02d}:{int(s%60):02d}"


VIDS = [('original', 'original_with_human_commentary.mp4'),
        ('blend_en', 'blend_live_en_synced.mp4'),
        ('blend_fr', 'blend_live_fr_synced.mp4'),
        ('eager_en', f'blend_eager_en{os.environ.get("BLEND_ARTIFACT_SUFFIX","")}_synced.mp4'),
        ('eager_fr', f'blend_eager_fr{os.environ.get("BLEND_ARTIFACT_SUFFIX","")}_synced.mp4'),
        ('eager_pt', f'blend_eager_pt{os.environ.get("BLEND_ARTIFACT_SUFFIX","")}_synced.mp4')]
have = {}
for name, src in VIDS:
    sp = BASE / src
    if sp.exists():
        shutil.copy2(sp, ROOT / f'{name}.mp4'); have[name] = True

# ---- pre-match data panel (everything the system knows before kickoff) ----
def prematch_text():
    sr = json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
    se = sr['lineups'].get('sport_event', {})
    ctx = se.get('sport_event_context', {})
    ven = se.get('venue', {})
    refs = (se.get('sport_event_conditions', {}) or {}).get('referees', [])
    comps = {c['qualifier']: c for c in se.get('competitors', [])}
    L = []
    A = L.append
    A("PRE-MATCH DATA (everything known to the system before kickoff)")
    A("Reviewer: naming/variation advice welcome — what should we call each team,")
    A("which nicknames are broadcast-appropriate, which player name forms to prefer?")
    A("=" * 64)
    A(f"Competition : {ctx.get('competition',{}).get('name','?')} {ctx.get('season',{}).get('year','')} — matchday {ctx.get('round',{}).get('number','?')}")
    A(f"Kickoff     : {se.get('start_time','?')}")
    A(f"Venue       : {ven.get('name','?')}, {ven.get('city_name','?')} (capacity {ven.get('capacity','?')})")
    for r in refs:
        A(f"Referee     : {r.get('name')}  ({r.get('type','').replace('_',' ')})")
    A("")
    A("CLIP CONTEXT (verified from on-screen scoreboard)")
    A("  second half, ~77th minute, score 1-1, ~13 minutes of normal time left")
    A("")
    for q, kit in (('home', 'red shirts/shorts, white numbers'),
                   ('away', 'olive/dark-green shirts, gold numbers')):
        c = comps.get(q, {})
        A(f"{q.upper():5s}: {c.get('name','?')}  [abbrev {c.get('abbreviation','?')}, {c.get('country','')}]")
        A(f"       kit: {kit}")
        A( "       APPROVED reference forms (R11): " + ("Mainz / FSV Mainz / the hosts / the home side / the reds" if q=='home' else "Union / Union Berlin / the visitors / the away side / the men in green"))
        A("")
    A("LINEUPS (number, name, position, starter)")
    for comp in sr['lineups']['lineups']['competitors']:
        A(f"--- {comp.get('name','?')} ---")
        players = sorted(comp.get('players', []), key=lambda p: (not p.get('starter'), int(p.get('jersey_number') or 99)))
        for pl in players:
            A(f"  #{str(pl.get('jersey_number','?')):>2s} {pl.get('name','?'):28s} {str(pl.get('position') or pl.get('type') or ''):16s}{' (starter)' if pl.get('starter') else ''}")
        A("")
    A("FRENCH GLOSSARY IN FORCE (reviewer-extendable in tuning_rules.yaml)")
    A("  dernier tiers -> les trente derniers metres · sonder -> tenter/essayer")
    A("  moment calme -> temps faible · returning players -> 'sont revenus' · Mainz -> Mayence ok")
    return "\n".join(L)

son = load(BASE / 'soniox_live_short.jsonl')   # A
oai = load(BASE / 'oai_col.jsonl')             # B
trk = load(BASE / 'tracker_col.jsonl')         # C
def _rep(name):
    f = BASE / name
    return json.loads(f.read_text()) if f.exists() else {}
ART = os.environ.get('BLEND_ARTIFACT_SUFFIX', '')   # '' or '_6s'
V4 = os.environ.get('PAGE_LAYOUT', '') == 'v4'        # EN/FR/PT, no legacy safe cols
FEEDBACK_VERSION = os.environ.get('FEEDBACK_VERSION', '')   # e.g. 'v4' enables the UI
rep_e = _rep(f'latency_report_eager{ART}.json')
rep_s = _rep('latency_report.json')
DELAY = rep_e.get('fixed_delay_s') or rep_s.get('fixed_delay_s') or 10.0
SURV = rep_e.get('survival_rate')

blend = load(BASE / 'commentary_blend_live.jsonl')
eager = load(BASE / f'commentary_blend_live_eager{ART}.jsonl')

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
def add_blend(rows_src, col_en, col_fr):
    for r in rows_src:
        if r.get('dropped'):
            continue      # missed the live buffer — never heard, never shown
        t = float(r.get('video_time_s', 0))
        items.append((t, col_en, html.escape(r.get('text', ''))))
        fr = r.get('fr') or ''
        if fr:
            items.append((t, col_fr, html.escape(fr)))
if V4:
    def add_v4(rows_src):
        for r in rows_src:
            if r.get('dropped'):
                continue
            t = float(r.get('video_time_s', 0))
            items.append((t, 3, html.escape(r.get('text', ''))))
            if r.get('fr'):
                items.append((t, 4, html.escape(r['fr'])))
            if r.get('pt'):
                items.append((t, 5, html.escape(r['pt'])))
    add_v4(eager)
    NCOLS = 6
else:
    add_blend(blend, 3, 5)
    add_blend(eager, 4, 6)
    NCOLS = 7

items.sort(key=lambda x: x[0])
rows = []
for t, col, cell in items:
    if not rows or t - rows[-1]['t0'] > ROW_MERGE_S:
        rows.append({'t': t, 't0': t, 'cells': [[] for _ in range(NCOLS)]})
    rows[-1]['cells'][col].append(cell)

def cell_html(cs):
    return "".join(f"<div class='ln'>{c}</div>" for c in cs) if cs else "<div class='hole'></div>"

body = ""
for row in rows:
    tds = "".join(f"<div class='cell'>{cell_html(row['cells'][i])}</div>" for i in range(NCOLS))
    body += (f"<div class='row' data-t='{row['t']:.2f}'><div class='tc'>{fmt(row['t'])}</div>{tds}</div>")

tabs = []
if have.get('original'): tabs.append("<button data-src='./original.mp4'>Original broadcast</button>")
if V4:
    tabs.append("<button data-src='./eager_en.mp4' class='active'>AI · English</button>")
    if have.get('eager_fr'): tabs.append("<button data-src='./eager_fr.mp4'>AI · French</button>")
    if have.get('eager_pt'): tabs.append("<button data-src='./eager_pt.mp4'>AI · Portuguese</button>")
else:
    tabs.append("<button data-src='./blend_en.mp4' class='active'>EN · safe</button>")
    if have.get('eager_en'): tabs.append("<button data-src='./eager_en.mp4'>EN · eager</button>")
    if have.get('blend_fr'): tabs.append("<button data-src='./blend_fr.mp4'>FR · safe</button>")
    if have.get('eager_fr'): tabs.append("<button data-src='./eager_fr.mp4'>FR · eager</button>")

FEEDBACK_JS = """
// ---------------- cell-level reviewer feedback ----------------
const FBV = '__FBV__';
const pending = [];
const bar = document.createElement('div'); bar.className='fbbar';
bar.innerHTML = `<span>Reviewer:</span><input id='fbname' placeholder='your name'>
 <span><span class='cnt' id='fbcnt'>0</span> unsent comments</span>
 <button id='fbsubmit'>Submit feedback</button>
 <button id='fbtrigger'>Close round & trigger next version</button>
 <span id='fbstatus' style='color:#8a8a8a'></span>`;
document.body.appendChild(bar);
document.body.style.paddingBottom='54px';
const nameEl = bar.querySelector('#fbname');
nameEl.value = localStorage.getItem('fb_reviewer') || '';
nameEl.addEventListener('change', ()=>localStorage.setItem('fb_reviewer', nameEl.value));
fetch('/blend_rounds').then(r=>r.json()).then(j=>{
  const st=(j.rounds&&j.rounds[FBV]||{}).status;
  if(st!=='open'){
    const d=document.createElement('div'); d.className='fbclosed';
    d.textContent=`Round ${FBV} is CLOSED — comments here will be rejected. Open round: ${j.current}`;
    document.querySelector('.top').appendChild(d);
  }
}).catch(()=>{});
const TAGS=['wrong fact','repetition','language','naming','timing','👍 good'];
const HEAD=[...document.querySelectorAll('.head>div')].slice(1).map(d=>d.textContent.trim());
document.querySelectorAll('.row .cell').forEach((cell)=>{
  cell.classList.add('fb-target');
  cell.addEventListener('click', ev=>{
    if(ev.target.closest('.fbbox')) return;
    const open=cell.querySelector('.fbbox'); if(open){open.remove();return;}
    document.querySelectorAll('.fbbox').forEach(b=>b.remove());
    const row=cell.closest('.row'); const t=row?row.dataset.t:'?';
    // capture column + clean cell text NOW, before injecting the box
    const cells=[...row.querySelectorAll('.cell')];
    const col=cells.indexOf(cell);                    // 0-based among data columns
    const colName=HEAD[col]||('col'+col);
    const cellText=cell.innerText.slice(0,300);       // snapshot, box not yet added
    const box=document.createElement('div'); box.className='fbbox';
    box.innerHTML=`<div style='font-size:10px;color:#7dd3fc;margin-bottom:4px'>${colName} @ ${t}s</div>
      <textarea placeholder='comment on this cell…'></textarea>
      <div class='tags'>${TAGS.map(x=>`<span>${x}</span>`).join('')}</div>
      <div class='row2'><button class='add'>Add</button></div>`;
    box.querySelectorAll('.tags span').forEach(sp=>sp.addEventListener('click',()=>sp.classList.toggle('on')));
    box.querySelector('.add').addEventListener('click',()=>{
      const txt=box.querySelector('textarea').value.trim();
      const tags=[...box.querySelectorAll('.tags span.on')].map(x=>x.textContent);
      if(!txt&&!tags.length) return;
      pending.push({t:parseFloat(t), col:col, column:colName, cell_text:cellText, tags:tags, comment:txt});
      cell.classList.add('has-fb');
      document.getElementById('fbcnt').textContent=pending.length;
      box.remove();
    });
    cell.appendChild(box); box.querySelector('textarea').focus();
    ev.stopPropagation();
  });
});
bar.querySelector('#fbsubmit').addEventListener('click',()=>{
  const who=nameEl.value.trim(); const st=document.getElementById('fbstatus');
  if(!who){st.textContent='enter your name first';return;}
  if(!pending.length){st.textContent='nothing to submit';return;}
  fetch('/blend_feedback',{method:'POST',body:JSON.stringify({reviewer:who,version:FBV,items:pending})})
    .then(r=>r.json().then(j=>({s:r.status,j})))
    .then(({s,j})=>{ if(s===200){st.textContent=`✔ ${j.stored} comments submitted — thank you`;pending.length=0;
                       document.getElementById('fbcnt').textContent='0';}
                     else st.textContent=`✖ ${j.error||'failed'} ${j.hint||''}`; })
    .catch(()=>st.textContent='✖ network error');
});
bar.querySelector('#fbtrigger').addEventListener('click',()=>{
  const who=nameEl.value.trim(); const st=document.getElementById('fbstatus');
  if(!who){st.textContent='enter your name first';return;}
  if(pending.length){st.textContent='submit your comments first';return;}
  if(!confirm(`Close review round ${FBV} and trigger the next version?\nOnly ONE person should do this.`)) return;
  const pin=prompt('Trigger PIN:'); if(!pin) return;
  fetch('/blend_trigger',{method:'POST',body:JSON.stringify({version:FBV,pin:pin,triggered_by:who})})
    .then(r=>r.json().then(j=>({s:r.status,j})))
    .then(({s,j})=>{ st.textContent = s===200? `✔ round ${FBV} closed (${j.submissions} submissions) — next version will be announced`
                                             : `✖ ${j.error||'failed'}`; })
    .catch(()=>st.textContent='✖ network error');
});
""".replace('__FBV__', FEEDBACK_VERSION)

doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>AI live commentary — Mainz vs Union</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:14px}}
h1{{font-size:18px}} .sub{{color:#8a8a8a;font-size:12.5px;margin:3px 0 10px;max-width:1100px}}
.top{{position:sticky;top:0;background:#0a0a0a;z-index:6;padding-bottom:10px;border-bottom:1px solid #222}}
video{{width:100%;max-width:640px;display:block;border-radius:6px;background:#000}}
.delaybar{{max-width:1060px;margin:0 auto 8px;padding:7px 12px;border:1px solid #14532d;background:#052e16;
  color:#86efac;border-radius:6px;font-size:12.5px;text-align:center}}
.delaybar b{{color:#4ade80}}
.vidrow{{display:flex;gap:12px;justify-content:center;align-items:stretch;margin-bottom:8px;flex-wrap:wrap}}
.pmwrap{{display:flex;flex-direction:column;width:400px;max-width:95vw}}
.pmh{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#fbbf24;padding:2px 2px 6px}}
.pmh .pmn{{color:#8a8a8a;text-transform:none;font-weight:500;letter-spacing:0}}
.pm{{flex:1;min-height:280px;background:#0e0e0e;color:#c8c8c8;border:1px solid #262626;border-radius:6px;
     padding:10px;font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;resize:vertical;
     overflow-y:auto;white-space:pre}}
.tabs{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}}
.tabs button{{background:#1a1a1a;color:#aaa;border:1px solid #2a2a2a;padding:7px 15px;font-size:12.5px;border-radius:18px;cursor:pointer;font-weight:600}}
.tabs button.active{{background:#052e16;color:#4ade80;border-color:#166534}}
.wrap{{margin-top:12px;border:1px solid #1f1f1f;border-radius:7px;overflow-x:auto}}
.grid{{min-width:1240px}}
.head,.row{{display:grid;grid-template-columns:50px repeat({NCOLS},1fr)}}
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
.cell.fb-target:hover{{outline:1px dashed #4ade80;cursor:context-menu}}
.cell.has-fb{{box-shadow:inset 3px 0 0 #fbbf24}}
.fbbox{{margin-top:5px;background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:7px}}
.fbbox textarea{{width:100%;min-height:44px;background:#0a0f18;color:#dbeafe;border:1px solid #1e3a5f;border-radius:4px;padding:5px;font-size:11.5px}}
.fbbox .tags{{display:flex;gap:5px;flex-wrap:wrap;margin:5px 0}}
.fbbox .tags span{{border:1px solid #334155;border-radius:10px;padding:1px 8px;font-size:10px;color:#94a3b8;cursor:pointer}}
.fbbox .tags span.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
.fbbox .row2{{display:flex;gap:6px;justify-content:flex-end}}
.fbbox button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer}}
.fbbar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;z-index:20;
       display:flex;gap:10px;align-items:center;justify-content:center;padding:8px;font-size:12.5px}}
.fbbar input{{background:#0a0f18;color:#dbeafe;border:1px solid #1e3a5f;border-radius:4px;padding:5px 8px;font-size:12px;width:130px}}
.fbbar .cnt{{color:#fbbf24;font-weight:700}}
.fbbar button{{border:0;border-radius:5px;padding:6px 14px;font-size:12px;cursor:pointer;font-weight:600}}
#fbsubmit{{background:#14532d;color:#86efac}}
#fbtrigger{{background:#450a0a;color:#fca5a5}}
.fbclosed{{background:#450a0a;color:#fca5a5;text-align:center;padding:6px;border-radius:6px;margin:8px auto;max-width:900px;font-size:12.5px}}
</style></head><body>
<div class='top'>
  <h1>AI live commentary — Mainz vs Union Berlin{(' · ' + VERSION) if VERSION else ''}</h1>
  <p class='sub'>One commentary track built live over the SRT feed and voiced in English and French.
  <b>STT</b> = short, high-confidence phrases from the broadcast audio. <b>Vision</b> = what the
  system reads from the video frames (possession, events), shown with its own confidence.
  <b>Tracker</b> = objective on-pitch positions and shape. The blend fuses them into one spoken
  line, preferring real phrases and filling the gaps. Holes are where a signal stayed silent.
  Pick an audio track; click any row to seek.</p>
  <div class='delaybar'>⏱ Generated LIVE with a fixed <b>{DELAY:.0f}-second broadcast delay</b> —
  every line you hear is spoken about the moment on screen (on-play or dropped, never late){f" · {SURV*100:.0f}% of candidate lines made the window this run" if SURV else ''}.</div>
  <div class='vidrow'>
    <video id='v' controls preload='metadata' src='{'./eager_en.mp4' if V4 else './blend_en.mp4'}'></video>
    <div class='pmwrap'>
      <div class='pmh'>Pre-match data <span class='pmn'>— advise on team/player naming variation</span></div>
      <textarea class='pm' readonly spellcheck='false'>{html.escape(prematch_text())}</textarea>
    </div>
  </div>
  <div class='tabs'>{''.join(tabs)}</div>
</div>
<div class='wrap'><div class='grid'>
  {"<div class='head'><div class='h0'>time</div><div class='hA'>STT</div><div class='hB'>Vision</div><div class='hC'>Tracker</div><div class='hEN'>English</div><div class='hFR'>French</div><div class='hFR'>Portuguese</div></div>" if V4 else "<div class='head'><div class='h0'>time</div><div class='hA'>STT</div><div class='hB'>Vision</div><div class='hC'>Tracker</div><div class='hEN'>EN · safe</div><div class='hEN'>EN · eager</div><div class='hFR'>FR · safe</div><div class='hFR'>FR · eager</div></div>"}
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

{FEEDBACK_JS if FEEDBACK_VERSION else ''}
</script></body></html>"""
(ROOT / 'index.html').write_text(doc)
print(f"wrote {ROOT/'index.html'}  rows={len(rows)}  A={len(son)} B={len(oai)} C={len(trk)} "
      f"safe={len(blend)} eager={len(eager)}")
