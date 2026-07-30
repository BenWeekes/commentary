#!/usr/bin/env python3
"""Sentence-level ASR comparison page: Soniox v5 vs Gemini 3.5 Transcribe Live.

URL shape: /experiments/asr_compare/<clipid>/ — video on top, one row per sentence:
timestamp | soniox sentence | soniox final latency | gemini sentence | gemini final
latency | notes (auto: fastest + accuracy call; contenteditable so the reviewer can
overrule — saved to the feedback server, version 'asrcompare').

Latency is reference-free: both engines were real-time streamed, so each engine's
sentence-final latency = wall clock when its last token/event finalized minus the
audio end time of the sentence (Soniox per-token end_ms, same anchor for Gemini via
word alignment).

Usage: .venv/bin/python asr_compare_page.py [gemini_tag] [clipid]
"""
import difflib, html, json, re, sys
from pathlib import Path

SCR = Path('/tmp/claude-1000/-home-ubuntu-commentary/07cecf7f-8b44-4628-bbe3-905461a6d22c/scratchpad/gemini_asr')
V2V = Path('/home/ubuntu/commentary/experiments/v2v_5min_slice')
BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
TAG = sys.argv[1] if len(sys.argv) > 1 else 'adapt3'
CLIPID = sys.argv[2] if len(sys.argv) > 2 else 'm05_uni_5min'
VIDEO_SRC = '/experiments/ai_commentator/blend_v7_10s/original.mp4'


def norm_w(w):
    return re.sub(r"[^a-z0-9']", '', w.lower())


def soniox_words():
    """(display, norm, start_s, end_s, final_wall_t) per word."""
    toks = [json.loads(l) for l in open(V2V / 'soniox_v5_tokens.jsonl') if l.strip()]
    fin = [t for t in toks if t.get('is_final') and t.get('translation_status') == 'original']
    words, cur = [], None
    for t in fin:
        txt = t['text']
        if txt.startswith(' ') and cur:
            words.append(cur); cur = None
        if cur is None:
            cur = [txt.strip(), None, t['start_ms'] / 1000.0, t['end_ms'] / 1000.0, t['wall_t']]
        else:
            cur[0] += txt
            cur[3] = t['end_ms'] / 1000.0
            cur[4] = max(cur[4], t['wall_t'])
        cur[1] = norm_w(cur[0])
    if cur:
        words.append(cur)
    return [w for w in words if w[1]]


def gemini_words(tag):
    """(display, norm, arrival_s) per word — arrival is the event's wall time."""
    ev = json.load(open(SCR / f'{tag}_events.json'))
    words = []
    for arr, txt in ev:
        for w in txt.split():
            if norm_w(w):
                words.append((w, norm_w(w), arr))
    return words


def sentences(son):
    """Split soniox word list into sentence spans [(i0, i1), ...] on ./?/!"""
    spans, start = [], 0
    for i, w in enumerate(son):
        if re.search(r'[.?!]["\']?$', w[0]) or i == len(son) - 1:
            spans.append((start, i + 1)); start = i + 1
    return [s for s in spans if s[1] > s[0]]


def roster_names():
    try:
        sys.path.insert(0, str(BASE))
        import run_blend_live as B
        return {norm_w(p.split()[-1]) for p in B.ALL_PLAYERS} | {norm_w(p) for p in B.ALL_PLAYERS}
    except Exception:
        return set()


def main():
    son = soniox_words()
    gem = gemini_words(TAG)
    spans = sentences(son)
    roster = roster_names()

    # map every gemini word to a soniox word index (insertion point for gemini-only)
    sm = difflib.SequenceMatcher(None, [w[1] for w in gem], [w[1] for w in son])
    g2s = {}
    for op, g1, g2, s1, s2 in sm.get_opcodes():
        for j in range(g1, g2):
            if op == 'equal':
                g2s[j] = s1 + (j - g1)
            else:                        # replace/delete: anchor to the soniox span start
                g2s[j] = min(s1 + (j - g1), max(s1, s2 - 1)) if s2 > s1 else s1
    rows = []
    for (i0, i1) in spans:
        s_words = son[i0:i1]
        s_text = ' '.join(w[0] for w in s_words)
        t_start, audio_end = s_words[0][2], s_words[-1][3]
        s_lat = max(w[4] for w in s_words) - audio_end
        g_idx = [j for j in range(len(gem)) if i0 <= g2s.get(j, -1) < i1]
        g_text = ' '.join(gem[j][0] for j in g_idx)
        g_lat = (max(gem[j][2] for j in g_idx) - audio_end) if g_idx else None

        s_norm = [w[1] for w in s_words]
        g_norm = [gem[j][1] for j in g_idx]
        notes = []
        if not g_idx:
            notes.append('Gemini: NOTHING (coverage hole — same spot all 3 runs). Soniox by default.')
        elif s_norm == g_norm:
            notes.append('Transcripts agree.')
        else:
            d = difflib.SequenceMatcher(None, g_norm, s_norm)
            diffs = []
            for op, a1, a2, b1, b2 in d.get_opcodes():
                if op == 'equal':
                    continue
                gs = ' '.join(gem[g_idx[k]][0] for k in range(a1, min(a2, len(g_idx))))
                ss = ' '.join(w[0] for w in s_words[b1:b2])
                diffs.append((ss, gs))
            shown = '; '.join(f"S:\u2018{a or '—'}\u2019 vs G:\u2018{b or '—'}\u2019" for a, b in diffs[:3])
            notes.append(f"Differs — {shown}" + (f" (+{len(diffs)-3} more)" if len(diffs) > 3 else ''))
            s_hits = sum(1 for a, _ in diffs if any(norm_w(x) in roster for x in a.split()))
            g_hits = sum(1 for _, b in diffs if any(norm_w(x) in roster for x in b.split()))
            if s_hits > g_hits:
                notes.append('Roster names side with Soniox — likely Soniox right.')
            elif g_hits > s_hits:
                notes.append('Roster names side with Gemini — likely Gemini right.')
            else:
                notes.append('Accuracy unclear — please judge.')
        if g_lat is not None:
            faster = 'Soniox' if s_lat <= g_lat else 'Gemini'
            notes.append(f'Fastest: {faster} ({min(s_lat, g_lat):.1f}s vs {max(s_lat, g_lat):.1f}s).')
        rows.append({'t': t_start, 'stx': s_text, 'slat': s_lat,
                     'gtx': g_text, 'glat': g_lat, 'note': ' '.join(notes)})

    def mmss(t):
        return f"{int(t // 60)}:{t % 60:04.1f}"

    try:
        import run_blend_live as B
        roster_list = sorted(B.ALL_PLAYERS)
    except Exception:
        roster_list = []
    params_html = f"""<details open style='background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:10px'>
<summary style='cursor:pointer'><b>Test setup — roster + engine params</b> (click to fold)</summary>
<p><b>Roster passed to BOTH engines</b> ({len(roster_list)} players, Mainz + Union Berlin):<br>
<span style='color:#94a3b8'>{html.escape(', '.join(roster_list))}</span></p>
<table style='width:auto'><tr><th></th><th>Soniox</th><th>Gemini</th></tr>
<tr><td>model</td><td>stt-rt-v5 (websocket, real-time)</td><td>gemini-3.5-transcribe-live-preview (Live API, real-time)</td></tr>
<tr><td>audio</td><td>pcm_s16le 16 kHz mono, 100 ms chunks, real-time paced</td><td>pcm 16 kHz mono, 1024-frame chunks, real-time paced</td></tr>
<tr><td>language</td><td>language_hints=["en"]</td><td>language_auto (autodetect)</td></tr>
<tr><td>biasing</td><td>context.terms=&lt;roster&gt; + general: domain="Bundesliga football match commentary",
match="FSV Mainz vs Union Berlin", text="Live English football commentary … Mewa Arena."</td>
<td>adaptation_phrases=&lt;roster + venue terms&gt;</td></tr>
<tr><td>timestamps</td><td>per-token start_ms/end_ms from API</td><td>none from API — audio times inherited from
word-aligned Soniox tokens</td></tr>
<tr><td>run</td><td>archived 2026-07-06 run (fresh rerun pending API key)</td><td>fresh run "{TAG}" 2026-07-29/30
(3 runs, identical coverage)</td></tr></table>
<p><b>Latency definition</b>: seconds from the sentence's audio END (last word spoken, Soniox token end_ms)
to the wall-clock arrival of that engine's final transcript for the sentence. Both engines share the same
audio anchor, so the numbers are directly comparable. Anchoring at sentence end (not start) avoids
penalising long sentences.</p></details>"""

    both = [r for r in rows if r['glat'] is not None]
    n_agree = sum(1 for r in rows if r['note'].startswith('Transcripts agree'))
    s_fast = sum(1 for r in both if r['slat'] <= r['glat'])
    trs = ''
    for i, r in enumerate(rows):
        gl = f"{r['glat']:.1f}s" if r['glat'] is not None else '—'
        cls = ' class=hole' if r['glat'] is None else ''
        trs += f"""<tr{cls}><td><a href='#' onclick="seek({r['t']:.1f});return false">{mmss(r['t'])}</a></td>
<td>{html.escape(r['stx'])}</td><td class=lat>{r['slat']:.1f}s</td>
<td>{html.escape(r['gtx']) or '<i>—</i>'}</td><td class=lat>{gl}</td>
<td class=note contenteditable data-i={i}>{html.escape(r['note'])}</td></tr>\n"""

    page = f"""<meta charset=utf-8><title>ASR compare — {CLIPID}</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13px system-ui;margin:18px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #333;padding:5px;vertical-align:top;font-size:12.5px}}
th{{background:#161616;position:sticky;top:365px}}video{{width:640px;display:block;margin:8px 0;position:sticky;top:0;z-index:5;background:#000}}
a{{color:#7dd3fc}}.lat{{white-space:nowrap;text-align:right;color:#a3e635}}
.note{{color:#fbbf24;min-width:220px}}.note:focus{{outline:1px solid #3b82f6;background:#111827}}
tr.hole td{{background:#1a1010}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px;text-align:center}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:6px 16px;cursor:pointer}}</style>
<h2>ASR compare — Soniox v5 vs Gemini 3.5 Transcribe Live ({TAG}) — {CLIPID}</h2>
<p>Both engines streamed real-time with the full roster as biasing context. Latency = seconds from
audio end of the sentence to that engine's final transcript. Rows in <span style='color:#f87171'>red</span>
are Gemini coverage holes (deterministic across 3 runs). Sentence summary: {len(rows)} rows,
{n_agree} agree verbatim; where both produced output, Soniox was faster on {s_fast}/{len(both)}.
<b>Notes are editable</b> — click into one, correct it, then Save notes.</p>
{params_html}
<video id=v controls preload=metadata src="{VIDEO_SRC}"></video>
<table><tr><th>start</th><th>Soniox v5</th><th>final</th><th>Gemini live</th><th>final</th><th>notes (editable)</th></tr>
{trs}</table>
<div id=bar><span id=cnt>0 edited</span> <button onclick="submitN()">Save notes</button> <span id=st></span></div>
<script>
const ORIG={{}};document.querySelectorAll('.note').forEach(n=>{{ORIG[n.dataset.i]=n.textContent;
 n.addEventListener('input',()=>{{document.getElementById('cnt').textContent=
   [...document.querySelectorAll('.note')].filter(x=>x.textContent!==ORIG[x.dataset.i]).length+' edited';}});}});
function seek(t){{const v=document.getElementById('v');v.currentTime=Math.max(0,t-1.0);v.play();}}
function submitN(){{
  const items=[...document.querySelectorAll('.note')].filter(n=>n.textContent!==ORIG[n.dataset.i]).map(n=>{{
    const r=n.parentElement;
    return {{t:parseFloat(r.cells[0].innerText.split(':').reduce((m,s)=>60*m+ +s,0)),col:0,column:'ASR',
      profile:'asr',clip:'{CLIPID}',cell_text:(r.cells[1].innerText+' || '+r.cells[3].innerText).slice(0,250),
      tags:['note_edit'],comment:n.textContent.slice(0,500)}};}});
  if(!items.length){{document.getElementById('st').textContent=' nothing edited';return;}}
  fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:'ben',version:'asrcompare',items:items}})}})
    .then(r=>r.json()).then(j=>document.getElementById('st').textContent=' saved ('+(j.stored||j.error)+')')
    .catch(()=>document.getElementById('st').textContent=' network error');}}
</script>"""
    out = Path(f'/var/www/html/experiments/asr_compare/{CLIPID}')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(page)
    print(f"rows={len(rows)} agree={n_agree} soniox_faster={s_fast}/{len(both)} -> {out}/index.html")


if __name__ == '__main__':
    main()
