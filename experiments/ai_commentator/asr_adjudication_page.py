#!/usr/bin/env python3
"""Word-level ASR adjudication page: Gemini 3.5 Transcribe Live vs Soniox v5.

Aligns the two transcripts at word level (difflib), anchors every divergence to a
precise clip time via Soniox per-token timestamps, and renders verdict buttons so a
human can rule each dispute (submitted to the feedback server, version 'asrgemini').
Coverage holes (Gemini-missing stretches) are listed separately with the UNARY
model's text for the same window — proving the audio was transcribable.

Usage: .venv/bin/python asr_adjudication_page.py [gemini_tag]
"""
import difflib, html, json, re, sys
from pathlib import Path

SCR = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/gemini_asr')
V2V = Path('/home/ubuntu/commentary/experiments/v2v_5min_slice')
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
TAG = sys.argv[1] if len(sys.argv) > 1 else 'adapt'

STOP_EQ = {("gonna", "going to"), ("'s", "is")}


def norm_w(w):
    return re.sub(r"[^a-z0-9']", '', w.lower())


def soniox_words():
    toks = [json.loads(l) for l in open(V2V / 'soniox_v5_tokens.jsonl') if l.strip()]
    fin = [t for t in toks if t.get('is_final') and t.get('translation_status') == 'original']
    words = []                      # (display, norm, start_s)
    buf, t0 = '', None
    for t in fin:
        txt = t['text']
        if txt.startswith(' ') and buf:
            words.append((buf, norm_w(buf), t0)); buf, t0 = '', None
        if buf == '':
            t0 = t['start_ms'] / 1000.0
        buf += txt.strip() if buf == '' else txt
    if buf:
        words.append((buf, norm_w(buf), t0))
    return [w for w in words if w[1]]


def gemini_words(tag):
    ev = json.load(open(SCR / f'{tag}_events.json'))
    words = []
    for arr, txt in ev:
        for w in txt.split():
            if norm_w(w):
                words.append((w, norm_w(w), max(0.0, arr - 1.8)))
    return words


def gold_turns():
    return [json.loads(l) for l in open(BASE / 'gold_soniox_5min.jsonl') if l.strip()]


def gold_at(t, turns):
    for g in turns:
        if g['start_s'] - 1 <= t <= g['end_s'] + 1:
            return g['text']
    return ''


def main():
    son = soniox_words()
    gem = gemini_words(TAG)
    turns = gold_turns()
    unary = (SCR / 'unary_full.txt').read_text() if (SCR / 'unary_full.txt').exists() else ''
    sm = difflib.SequenceMatcher(None, [w[1] for w in gem], [w[1] for w in son])
    # reference-free latency: on AGREED word runs, both engines are anchored to the
    # same audio moment (soniox word start_s ~ end of speech for that word run);
    # gemini lag = its arrival wall-time minus that audio time. No gold involved.
    gem_lags = []
    for op, g1, g2, s1, s2 in sm.get_opcodes():
        if op == 'equal' and (g2 - g1) >= 3:
            audio_t = son[s2 - 1][2]              # last matched word's audio time
            arr = gem[g2 - 1][2] + 1.8            # undo display offset -> raw arrival
            if -1 < arr - audio_t < 15:
                gem_lags.append(arr - audio_t)
    gem_lags.sort()
    lat = None
    if gem_lags:
        lat = {'n': len(gem_lags), 'p50': round(gem_lags[len(gem_lags)//2], 2),
               'p90': round(gem_lags[int(len(gem_lags)*0.9)], 2), 'max': round(gem_lags[-1], 2)}
    disputes, holes, extras = [], [], []
    for op, g1, g2, s1, s2 in sm.get_opcodes():
        if op == 'equal':
            continue
        gtx = ' '.join(w[0] for w in gem[g1:g2])
        stx = ' '.join(w[0] for w in son[s1:s2])
        t = son[s1][2] if s1 < len(son) else (gem[g1][2] if g1 < len(gem) else 0)
        if op == 'replace':
            disputes.append({'t': round(t, 1), 'soniox': stx, 'gemini': gtx})
        elif op == 'insert':            # soniox-only
            (holes if (s2 - s1) >= 8 else disputes).append(
                {'t': round(t, 1), 'soniox': stx, 'gemini': '' if (s2 - s1) >= 8 else '(omitted)'})
        elif op == 'delete':            # gemini-only
            if (g2 - g1) >= 3:
                extras.append({'t': round(t, 1), 'gemini': gtx, 'soniox': ''})
    # merge adjacent disputes within 1.0s
    merged = []
    for d in disputes:
        if merged and d['t'] - merged[-1]['t'] <= 1.0:
            merged[-1]['soniox'] = (merged[-1]['soniox'] + ' ' + d.get('soniox', '')).strip()
            merged[-1]['gemini'] = (merged[-1]['gemini'] + ' ' + d.get('gemini', '')).strip()
        else:
            merged.append(dict(d))
    merged = [d for d in merged if norm_w(d['soniox'].replace(' ', '')) != norm_w(d['gemini'].replace(' ', ''))]

    def esc(x):
        return html.escape(x) if x else '<i>—</i>'

    def mmss(t):
        return f"{int(t // 60)}:{int(t % 60):02d}"

    rows = ''
    for i, d in enumerate(merged):
        gld = gold_at(d['t'], turns)
        rows += f"""<tr id=r{i}><td><a href='#' onclick="seek({d['t']});return false">{mmss(d['t'])}</a></td>
<td>{esc(d['soniox'])}</td><td>{esc(d['gemini'])}</td><td class=g>{esc(gld[:120])}</td>
<td><span class=vb data-i={i} data-v=soniox>Soniox</span><span class=vb data-i={i} data-v=gemini>Gemini</span>
<span class=vb data-i={i} data-v=both_wrong>Both wrong</span><span class=vb data-i={i} data-v=unsure>Unsure</span></td></tr>\n"""
    hrows = ''
    for h in holes:
        seg = h['soniox']
        # find matching unary snippet for the same words (proof of audibility)
        key = ' '.join(seg.split()[:5]).lower()
        pos = unary.lower().find(' '.join(seg.split()[:3]).lower())
        u = unary[max(0, pos - 10):pos + 160] if pos >= 0 else '(not located)'
        hrows += (f"<tr><td><a href='#' onclick=\"seek({h['t']});return false\">{mmss(h['t'])}</a></td>"
                  f"<td>{esc(seg[:220])}</td><td>{esc(u[:180])}</td></tr>\n")

    agree_words = sum(g2 - g1 for op, g1, g2, _, _ in sm.get_opcodes() if op == 'equal')
    metrics = {'tag': TAG, 'gemini_words': len(gem), 'soniox_words': len(son),
               'agreement_words': agree_words,
               'agreement_pct_of_soniox': round(100*agree_words/len(son), 1),
               'gemini_lag_on_agreed': lat, 'disputes': None, 'holes': None}
    page = f"""<meta charset=utf-8><title>ASR adjudication — Gemini live vs Soniox v5</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13px system-ui;margin:18px}}
table{{border-collapse:collapse;width:100%;margin-bottom:22px}}td,th{{border:1px solid #333;padding:5px;vertical-align:top;font-size:12.5px}}
th{{background:#161616}}video{{width:640px;display:block;margin:8px 0;position:sticky;top:0;z-index:5;background:#000}}
a{{color:#7dd3fc}}.g{{color:#8a8a8a}}
.vb{{display:inline-block;border:1px solid #334155;border-radius:9px;padding:1px 8px;margin:1px;cursor:pointer;font-size:11px;color:#94a3b8}}
.vb.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px;text-align:center}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:6px 16px;cursor:pointer}}</style>
<h2>ASR A/B (gold-free) — Gemini 3.5 Transcribe Live vs Soniox v5, roster given to both</h2>
<div style='background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:8px'>
Soniox {len(son)} words · Gemini {len(gem)} words · <b>agreement {round(100*agree_words/len(son),1)}%</b> of Soniox words
· Gemini finalize lag on agreed words (audio-anchored, no gold): p50 {lat['p50'] if lat else '?'}s / p90 {lat['p90'] if lat else '?'}s
· Soniox finalize lag (its own tokens): p50 1.36s / p90 3.27s</div>
<p>Method: agreement between two independent engines = presumed correct for both.
Every divergence below is yours to judge — click a time (audio seeks), pick a verdict, then <b>Submit verdicts</b>.
The gold column is CONTEXT ONLY (it is Soniox-derived, not truth).</p>
<video id=v controls preload=metadata src="blend_v7_10s/original.mp4"></video>
<h3>Disputes — both transcribed, differently ({len(merged)})</h3>
<table><tr><th>time</th><th>Soniox v5</th><th>Gemini live ({TAG})</th><th>gold (context)</th><th>your verdict</th></tr>
{rows}</table>
<h3>Gemini coverage holes — Soniox heard it, Gemini-live returned nothing ({len(holes)})</h3>
<p>Third column: the UNARY Gemini model's text for the same audio — proving these stretches are transcribable
(the live variant's finalization drops them, not the model's hearing).</p>
<table><tr><th>time</th><th>Soniox v5 heard</th><th>Gemini UNARY heard (same audio)</th></tr>
{hrows}</table>
<div id=bar><span id=cnt>0 verdicts</span> <button onclick="submitV()">Submit verdicts</button> <span id=st></span></div>
<script>
const V={{}};
function seek(t){{const v=document.getElementById('v');v.currentTime=Math.max(0,t-1.5);v.play();}}
document.querySelectorAll('.vb').forEach(b=>b.addEventListener('click',()=>{{
  const i=b.dataset.i;V[i]=b.dataset.v;
  document.querySelectorAll(`.vb[data-i='${{i}}']`).forEach(x=>x.classList.toggle('on',x===b));
  document.getElementById('cnt').textContent=Object.keys(V).length+' verdicts';}}));
function submitV(){{
  const rows=[...document.querySelectorAll('tr[id^=r]')];
  const items=Object.entries(V).map(([i,v])=>{{
    const r=rows[+i];const t=parseFloat(r.querySelector('a').textContent.split(':').reduce((m,s)=>60*m+ +s,0));
    return {{t:t,col:0,column:'ASR',profile:'asr',clip:'mainz_union_md33_76-81',
      cell_text:r.cells[1].innerText.slice(0,200)+' || '+r.cells[2].innerText.slice(0,150),
      tags:[v],comment:'asr adjudication {TAG}'}};}});
  fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:'ben',version:'asrgemini',items:items}})}})
    .then(r=>r.json()).then(j=>document.getElementById('st').textContent=' saved ('+(j.stored||j.error)+')')
    .catch(()=>document.getElementById('st').textContent=' network error');}}
</script>"""
    out = Path('/var/www/html/experiments/ai_commentator/asr_adjudication.html')
    out.write_text(page)
    metrics['disputes'] = len(merged); metrics['holes'] = len(holes)
    (SCR / f'{TAG}_ab_metrics.json').write_text(json.dumps(metrics, indent=1))
    print(json.dumps(metrics, indent=1))


if __name__ == '__main__':
    main()
