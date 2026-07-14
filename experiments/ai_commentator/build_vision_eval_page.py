#!/usr/bin/env python3
"""Vision/tracker eval — multi-test comparison + scoring pages.

Each TEST renders to its own URL: /experiments/vision_tracker_eval/<id>/index.html
plus a landing index listing all tests. Layout is a single TIME-ALIGNED GRID:
one page scroll moves every column together (rows are shared across columns and
keyed by video time), with a sticky video + header so you can watch/pause while
scrolling. Tick each sufficiently-accurate detection; one Submit posts all ticks
for the whole clip to the backend, keyed by test id + reviewer.

Usage: python build_vision_eval_page.py
"""
import json, html, shutil
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
ROOT = Path('/var/www/html/experiments/vision_tracker_eval')
ROOT.mkdir(parents=True, exist_ok=True)
VIDEO_SRC = BASE / 'original_with_human_commentary.mp4'
GOLD = BASE / 'gold_soniox_5min.jsonl'

# --- test registry: add a dict here to publish another test at its own URL ---
TESTS = [
    {
        'id': 'r1-detector',
        'title': 'Round 1 — events detector: vision vs tracker (4f/8f)',
        'blurb': 'Same events-detector prompt (shirt numbers, not names) on OpenAI & Gemini at 4 and 8 frames of history, plus the roboflow+OCR tracker. Soniox STT = real broadcaster reference.',
        'columns': [
            ('OpenAI 5.5 · 4f', 'events_gpt55.jsonl',           'oai4', '#34d399'),
            ('OpenAI 5.5 · 8f', 'events_gpt55_8f.jsonl',        'oai8', '#6ee7b7'),
            ('Gemini flash · 4f', 'events_gemini_flash.jsonl',  'gem4', '#60a5fa'),
            ('Gemini flash · 8f', 'events_gemini_flash_8f.jsonl','gem8', '#93c5fd'),
            ('Tracker · roboflow', 'events_tracker.jsonl',      'trk',  '#a78bfa'),
        ],
    },
]

CONF_COL = {'high': '#16a34a', 'medium': '#d97706', 'low': '#6b7280'}

def load_jsonl(p):
    return [json.loads(l) for l in open(p)] if Path(p).exists() else []

def fmt_ts(s):
    s = float(s); return f"{int(s//60):02d}:{int(s%60):02d}"

TEAM_NAME = {'home': 'Mainz', 'away': 'Union', 'contested': 'contested'}
# pitch zone (a LOCATION, not an action). home-relative: 'attacking' end = Union's goal.
THIRD_LABEL = {'home_defensive': 'back third', 'middle': 'midfield', 'home_attacking': 'final third'}

def det_html(d):
    """Show only meaningful signal: possession (real team → Mainz/Union), detected
    events, set-piece ball states, and (for the tracker) player counts + ball side.
    Mute phase; drop none/unknown/dead-ball/in-play filler."""
    if not isinstance(d, dict):
        return "<span class='err'>—</span>"
    parts = []
    p = d.get('possession', {}) or {}
    team = p.get('team'); third = p.get('third'); side = p.get('side')
    shirt = p.get('player_shirt_number'); conf = (p.get('confidence') or 'low')
    if team in ('home', 'away', 'contested'):
        s = f"#{shirt} " if shirt is not None else ""
        # third is NOT shown here — the LLMs guess it unreliably. It appears only
        # in the tracker chip, where it's grounded via pitch homography.
        parts.append(f"<span class='poss poss-{team}'>{s}{TEAM_NAME.get(team,team)}<i>{conf[:1]}</i></span>")
    for e in (d.get('events') or []):
        c = CONF_COL.get(e.get('confidence', 'low'), '#6b7280')
        tm = e.get('team'); tm = f" {TEAM_NAME.get(tm, tm)}" if tm and tm != 'unknown' else ''
        parts.append(f"<span class='ev' style='border-color:{c}'>{html.escape(str(e.get('type','?')))}{html.escape(tm)}</span>")
    bs = d.get('ball_state', '')
    if isinstance(bs, str) and bs.startswith('out_for_'):
        parts.append(f"<span class='sp'>↦ {html.escape(bs.replace('out_for_',''))}</span>")
    tr = d.get('tracker')
    if tr:
        bits = [f"{tr.get('players',0)} players"]
        if tr.get('mainz') or tr.get('union'):
            bits.append(f"{tr.get('mainz',0)} Mainz/{tr.get('union',0)} Union")
        if tr.get('ball_third'):
            bits.append(f"ball {THIRD_LABEL.get(tr['ball_third'], tr['ball_third'])} (homography)")
        elif tr.get('ball_side'):
            bits.append(f"ball {tr['ball_side']} of frame")
        parts.append(f"<span class='trk'>{html.escape(' · '.join(bits))}</span>")
    ph = d.get('phase', '')
    if ph and ph != 'open_play':
        parts.append(f"<span class='ph'>{html.escape(ph)}</span>")
    return ''.join(parts) if parts else "<span class='dot'>·</span>"


EVENT_TYPES = ("kick_off throw_in corner goal_kick free_kick penalty offside shot save goal "
               "yellow_card red_card foul handball tackle header cross substitution var_check "
               "injury_stoppage replay_starts half_time full_time referee_advantage").split()
PHASE_TYPES = ("open_play set_piece_setup stoppage replay crowd_shot manager_shot wide_stadium "
               "graphic_overlay unknown").split()

def build_test(test):
    tid = test['id']; cols = test['columns']
    outdir = ROOT / tid; outdir.mkdir(parents=True, exist_ok=True)

    # index detection records by rounded video time; collect the shared time grid
    by_src = {}; times = set()
    counts = {}
    for label, fname, cls, color in cols:
        recs = load_jsonl(BASE / fname)
        m = {}
        for r in recs:
            if 'detection' not in r: continue
            t = round(float(r['video_time_s']), 1)
            m[t] = r['detection']; times.add(t)
        by_src[cls] = m
        counts[cls] = len(m)
    # soniox turns placed at nearest grid time
    gold = load_jsonl(GOLD)
    son = {}
    grid = sorted(times)
    for g in gold:
        if not grid: break
        t = float(g.get('start_s', 0))
        nearest = min(grid, key=lambda x: abs(x - t))
        son.setdefault(nearest, []).append(g)
    # harvested short standalone Soniox utterances (<=4s, complete, high-conf)
    son_short = {}
    for u in load_jsonl(BASE / 'soniox_short.jsonl'):
        if not grid: break
        nearest = min(grid, key=lambda x: abs(x - float(u.get('video_time_s', 0))))
        son_short.setdefault(nearest, []).append(u)
    n_short = sum(len(v) for v in son_short.values())

    # header row
    col_defs = ([('son', 'Soniox full', '#fbbf24'), ('son2', f'Soniox short ≤4s ({n_short})', '#fcd34d')]
                + [(c[2], c[0], c[3]) for c in cols])
    hdr = "".join(f"<div class='cell' style='color:{color}'>{html.escape(lab)}</div>" for _, lab, color in col_defs)

    # body rows (one per grid time) — a single flex row spanning all columns
    rows = []
    for t in grid:
        cells = []
        # soniox
        st = son.get(t, [])
        son_html = "".join(f"<span class='spk spk-{x.get('speaker',0)}'>S{x.get('speaker',0)}</span>{html.escape(x.get('text',''))}" for x in st)
        cells.append(f"<div class='cell'>{son_html}</div>")
        # short standalone Soniox utterances (the phrase pool)
        ss = son_short.get(t, [])
        ss_html = "".join(f"<span class='short'>{html.escape(u.get('text',''))}"
                          f"<i>{u.get('dur')}s·c{u.get('conf')}</i></span>" for u in ss)
        cells.append(f"<div class='cell'>{ss_html}</div>")
        for _, fname, cls, color in cols:
            d = by_src[cls].get(t)
            if d is None:
                cells.append("<div class='cell'></div>")
            else:
                rid = f"{cls}_{t:.1f}"
                cells.append(f"<div class='cell'><label class='tk'><input type='checkbox' class='tick' data-id='{rid}'>"
                             f"<span>{det_html(d)}</span></label></div>")
        rows.append(f"<div class='brow' data-t='{t:.2f}'><div class='tcell'>{fmt_ts(t)}</div>{''.join(cells)}</div>")

    labels_js = json.dumps({c[2]: c[0] for c in cols})
    ncol = len(col_defs)
    tax_html = ("<details class='tax'><summary>all possible events / phases the detector can output</summary>"
                f"<div class='tx'><b>events:</b> {', '.join(EVENT_TYPES)}</div>"
                f"<div class='tx'><b>phase:</b> {', '.join(PHASE_TYPES)}</div>"
                "<div class='tx'><b>ball:</b> in_play, out_for_throw / corner / goal_kick, dead_ball_after_whistle, off_screen, unknown</div></details>")
    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>{html.escape(test['title'])}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5}}
a{{color:#60a5fa}}
.topbar{{position:sticky;top:0;background:#0a0a0a;z-index:10;border-bottom:1px solid #222;padding:8px 10px}}
.tbrow{{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}}
video{{width:340px;max-width:46vw;border-radius:6px;background:#000}}
h1{{font-size:15px;margin-bottom:3px}} .blurb{{font-size:11px;color:#888;max-width:520px}}
.rev{{font-size:12px;margin-top:6px}} .rev input{{background:#161616;border:1px solid #333;color:#eee;border-radius:5px;padding:4px 8px;font-size:12px;width:150px}}
.sub button{{background:#166534;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-weight:700;cursor:pointer;font-size:12px;margin-top:6px}}
.sub button:hover{{background:#15803d}} .sub .st{{font-size:11px;color:#9ca3af;margin-left:8px}}
.tally2{{font-size:11px;color:#cbd5e1;margin-top:5px}} .tally2 b{{color:#4ade80}} .tg2{{margin-right:10px;white-space:nowrap}}
.hdrrow{{display:flex;gap:0;position:sticky;top:0;background:#111;z-index:9;border-bottom:1px solid #2a2a2a}}
.hdrrow .cell{{font-weight:700;text-transform:uppercase;font-size:10.5px;letter-spacing:.3px;padding:7px 7px}}
.grid{{}}
.brow{{display:flex;gap:0;border-top:1px solid #141414;cursor:pointer;align-items:stretch;scroll-margin-top:250px}}
.brow:hover{{background:#141414}} .brow.active{{background:#16233a;box-shadow:inset 3px 0 0 #4ade80}}
.tcell{{width:46px;flex:none;font-family:ui-monospace,monospace;font-size:10px;color:#666;padding:5px 4px}}
.hdrrow .tcell{{color:#888}}
.cell{{flex:1;min-width:0;padding:5px 7px;border-left:1px solid #1a1a1a;font-size:12px;line-height:1.35;overflow-wrap:anywhere}}
.tk{{display:flex;gap:5px;align-items:flex-start;cursor:pointer}}
.tick{{width:14px;height:14px;flex:none;margin-top:1px;cursor:pointer;accent-color:#4ade80}}
label.tk.on{{background:#0c1f12;border-radius:4px}}
.spk{{font-size:9px;font-weight:700;padding:0 4px;border-radius:3px;margin-right:4px;background:#2d2400;color:#fbbf24}}
.phase{{font-size:9px;padding:1px 5px;border-radius:8px;background:#1f2937;color:#cbd5e1;margin-right:4px}}
.phase-open_play{{background:#052e16;color:#4ade80}} .phase-stoppage,.phase-replay,.phase-crowd_shot{{background:#3f1d1d;color:#fca5a5}}
.poss{{display:inline-block;font-size:11px;padding:0 5px;border-radius:4px;margin:1px 4px 1px 0;background:#1f2937;color:#e5e7eb}}
.poss-home{{background:#3a1414;color:#fca5a5}} .poss-away{{background:#14243a;color:#93c5fd}} .poss-contested{{background:#3a3214;color:#fcd34d}}
.poss i{{font-style:normal;opacity:.55;margin-left:3px;font-size:9px}}
.ev{{display:inline-block;font-size:10px;border:1px solid #444;border-radius:4px;padding:0 5px;margin:1px 3px 1px 0;color:#eee}}
.sp{{display:inline-block;font-size:10px;color:#fbbf24;margin:1px 4px 1px 0}}
.ph{{font-size:9px;color:#566;margin-left:2px}} .dot{{color:#2a2a2a}}
.trk{{display:inline-block;font-size:10px;color:#c4b5fd;background:#191430;border-radius:4px;padding:0 5px;margin:1px 4px 1px 0}}
.short{{display:block;color:#fde68a;font-size:11.5px;line-height:1.3;margin:1px 0}} .short i{{color:#8a7a4a;font-style:normal;font-size:9px;margin-left:5px}}
.legend2{{font-size:11px;color:#8a8a8a;margin-top:5px;max-width:640px;line-height:1.4}} .legend2 b{{color:#bbb}}
.tax{{font-size:11px;margin-top:5px;max-width:560px}} .tax summary{{cursor:pointer;color:#a78bfa;user-select:none}}
.tax .tx{{color:#8a8a8a;margin:3px 0;line-height:1.4}} .tax b{{color:#bbb}}
.follow{{font-size:11px;margin-left:10px;color:#9ca3af;cursor:pointer;user-select:none}}
</style></head><body>
<div class='topbar'>
  <div class='tbrow'>
    <video id='vid' controls preload='metadata' src='/experiments/vision_tracker_eval/original.mp4'></video>
    <div>
      <h1>{html.escape(test['title'])}</h1>
      <div class='blurb'>{html.escape(test['blurb'])} · <a href='/experiments/vision_tracker_eval/'>all tests</a></div>
      <div class='rev'>Reviewer: <input id='rev' placeholder='your name'>
        <label class='follow'><input type='checkbox' id='follow' checked> follow video</label>
        <span class='sub'><button onclick='submitScores()'>Submit whole-clip scores</button><span class='st' id='substatus'>tick accurate cells, name, submit</span></span></div>
      <div class='tally2' id='tally2'></div>
      <div class='legend2'>possession reads as <b>#shirt · Team · pitch-third</b> + conf(h/m/l). <b style='color:#fca5a5'>home = Mainz</b> (red), <b style='color:#93c5fd'>away = Union</b> (olive). the zone is <b>a location, not an action</b> — <b>back third / midfield / final third</b> (final = Union's-goal end); if unknown it falls back to <i>frame</i> side. Static setups still show phase (e.g. <i>set_piece_setup</i>) + the event (e.g. <i>free_kick</i>). Tracker column shows player counts + ball position (it can't read shirt numbers at this resolution).</div>
      {tax_html}
    </div>
  </div>
</div>
<div class='hdrrow'><div class='tcell'>time</div>{hdr}</div>
<div class='grid'>{''.join(rows)}</div>
<script>
const vid=document.getElementById('vid');
const LAB={labels_js};
const TID={json.dumps(tid)};
const TKEY='vte_ticks_'+TID;
let ticks=JSON.parse(localStorage.getItem(TKEY)||'{{}}');
const revEl=document.getElementById('rev');
revEl.value=localStorage.getItem('vte_rev')||'';
revEl.addEventListener('input',()=>localStorage.setItem('vte_rev',revEl.value));
document.querySelectorAll('.tick').forEach(cb=>{{
  const id=cb.dataset.id; cb.checked=!!ticks[id];
  cb.closest('.tk').classList.toggle('on',cb.checked);
  cb.addEventListener('click',e=>e.stopPropagation());
  cb.addEventListener('change',()=>{{
    if(cb.checked)ticks[id]=1; else delete ticks[id];
    localStorage.setItem(TKEY,JSON.stringify(ticks));
    cb.closest('.tk').classList.toggle('on',cb.checked); tally();}});
}});
document.querySelectorAll('.brow').forEach(r=>r.addEventListener('click',()=>{{
  vid.currentTime=Math.max(0,parseFloat(r.dataset.t)-0.3);vid.play();}}));
let brows=[...document.querySelectorAll('.brow')];
const followEl=document.getElementById('follow');
let lastAct=null;
function hl(){{const t=vid.currentTime;let act=null;
  for(const r of brows){{if(parseFloat(r.dataset.t)<=t+0.05)act=r;r.classList.remove('active');}}
  if(act){{act.classList.add('active');
    if(act!==lastAct){{lastAct=act; if(followEl&&followEl.checked) act.scrollIntoView({{block:'center',behavior:'smooth'}});}}}}}}
vid.addEventListener('timeupdate',hl);
function counts(){{const a={{}};
  document.querySelectorAll('.tick').forEach(cb=>{{const s=cb.dataset.id.split('_')[0];
    a[s]=a[s]||{{tot:0,ok:0}};a[s].tot++;if(ticks[cb.dataset.id])a[s].ok++;}});return a;}}
function tally(){{const a=counts();let h='';
  for(const s in a){{const acc=a[s].tot?Math.round(100*a[s].ok/a[s].tot):0;
    h+=`<span class='tg2'>${{LAB[s]||s}}: <b>${{a[s].ok}}</b>/${{a[s].tot}} (${{acc}}%)</span>`;}}
  document.getElementById('tally2').innerHTML=h;}}
tally();
async function submitScores(){{
  const rev=revEl.value.trim();const st=document.getElementById('substatus');
  if(!rev){{st.textContent='⚠ enter your name first';return;}}
  st.textContent='submitting…';
  try{{const r=await fetch('/vte_submit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{test:TID,reviewer:rev,ticks:ticks,summary:counts(),ts:Date.now()}})}});
    st.textContent=r.ok?('submitted ✓ thanks '+rev):('failed ('+r.status+'); saved locally');
  }}catch(e){{st.textContent='no server; saved locally in browser';}}
}}
</script></body></html>"""
    (outdir / 'index.html').write_text(doc)
    return {'id': tid, 'title': test['title'], 'blurb': test['blurb'], 'rows': len(grid), 'counts': counts}


def main():
    if VIDEO_SRC.exists() and not (ROOT / 'original.mp4').exists():
        shutil.copy2(VIDEO_SRC, ROOT / 'original.mp4')
    built = [build_test(t) for t in TESTS]
    # landing index
    cards = ''.join(
        f"<div class='card'><a href='./{b['id']}/'><b>{html.escape(b['title'])}</b></a>"
        f"<div class='m'>{b['rows']} timeline rows · "
        + " · ".join(f"{k}:{v}" for k, v in b['counts'].items()) + f"</div>"
        f"<div class='bl'>{html.escape(b['blurb'])}</div></div>"
        for b in built)
    landing = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>Vision/Tracker eval — tests</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:20px;max-width:800px;margin:0 auto}}
h1{{font-size:20px;margin-bottom:12px}} a{{color:#60a5fa;text-decoration:none}} a:hover{{text-decoration:underline}}
.card{{background:#111;border:1px solid #222;border-radius:8px;padding:14px;margin-bottom:12px}}
.card b{{font-size:15px}} .m{{font-size:11px;color:#7dd3fc;margin:4px 0;font-family:ui-monospace,monospace}} .bl{{font-size:12px;color:#999}}</style>
</head><body><h1>Vision vs Tracker — evaluation tests</h1>{cards}</body></html>"""
    (ROOT / 'index.html').write_text(landing)
    print(f"landing: /experiments/vision_tracker_eval/")
    for b in built:
        print(f"  test '{b['id']}': /experiments/vision_tracker_eval/{b['id']}/  ({b['rows']} rows, {b['counts']})")


if __name__ == '__main__':
    main()
