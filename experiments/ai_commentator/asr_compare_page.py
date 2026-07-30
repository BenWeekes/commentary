#!/usr/bin/env python3
"""Sentence-level ASR comparison page: Soniox v5 vs Gemini 3.5 Transcribe Live.

URL shape: /experiments/asr_compare/<clipid>/ — video on top, one row per sentence:
timestamp | soniox sentence | soniox final latency | gemini sentence | gemini final
latency | notes.

Notes color code (per Ben's spec):
  GREEN  = sure Gemini wins (transcripts agree, Gemini finalized faster)
  RED    = sure Soniox wins (transcripts agree + Soniox faster, or Gemini hole)
  ORANGE = transcripts differ -> reviewer must judge; verdict buttons in the row
           turn it green/red and the verdict + note are saved (version 'asrcompare').
Saved verdicts/notes are re-applied when this generator reruns, so the page
persists reviewer state across regenerations.

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


def roster_display():
    """Player display names from the blend roster (ALL_PLAYERS is a list of dicts)."""
    sys.path.insert(0, str(BASE))
    import run_blend_live as B
    return sorted(p['name'] if isinstance(p, dict) else str(p) for p in B.ALL_PLAYERS)


def roster_parts():
    """All normed name segments — 'Woo-yeong' -> {'wooyeong','woo','yeong'}."""
    parts = set()
    for n in roster_display():
        parts.add(norm_w(n))
        for seg in re.split(r'[\s-]+', n):
            if norm_w(seg):
                parts.add(norm_w(seg))
    return {p for p in parts if p}


def roster_score(word, parts):
    """Closeness to the roster: 1.0 = exact name, else best fuzzy ratio (>=0.75)
    against any name part (Jeong~yeong 0.8), 0 = nothing close."""
    w = norm_w(word)
    if not w:
        return 0.0
    if w in parts:
        return 1.0
    if len(w) < 4:
        return 0.0
    best = max((difflib.SequenceMatcher(None, w, p).ratio()
                for p in parts if len(p) >= 4), default=0.0)
    return best if best >= 0.75 else 0.0


def load_saved():
    """Reviewer verdicts/notes already stored for this clip: {t_key: {...latest...}}."""
    saved = {}
    for src in [BASE / 'feedback/asrcompare/comments.jsonl',
                BASE / 'feedback/asrcompare/late/comments.jsonl']:
        if not src.exists():
            continue
        for line in open(src, encoding='utf-8'):
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get('reviewer') == 'selftest':      # API round-trip probes, never applied
                continue
            for it in rec.get('items', []):
                if it.get('clip') != CLIPID:
                    continue
                saved.setdefault(round(float(it.get('t', 0)), 1), []).append(
                    {'tags': it.get('tags', []), 'comment': it.get('comment', '')})
    return saved


def main():
    son = soniox_words()
    gem = gemini_words(TAG)
    spans = sentences(son)
    roster = roster_parts()
    saved = load_saved()

    # map every gemini word to a soniox word index (insertion point for gemini-only)
    sm = difflib.SequenceMatcher(None, [w[1] for w in gem], [w[1] for w in son])
    g2s = {}
    for op, g1, g2, s1, s2 in sm.get_opcodes():
        for j in range(g1, g2):
            if op == 'equal':
                g2s[j] = s1 + (j - g1)
            else:
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
            verdict = 'soniox'
            notes.append('Gemini: NOTHING (coverage hole — same spot all 3 runs). Soniox by default.')
        elif s_norm == g_norm:
            verdict = 'soniox' if s_lat <= g_lat else 'gemini'
            notes.append('Transcripts agree — decided on latency.')
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

            def roster_call(ss, gs):
                """'soniox'/'gemini' ONLY for a safe mishearing pair: both sides short,
                phonetically close (same utterance heard differently, e.g. Jeong/Jung),
                and one side strictly closer to a roster name. Misaligned fragments,
                omissions, and first-name mentions (roster is surname-only) stay orange."""
                if not ss or not gs or len(ss.split()) > 2 or len(gs.split()) > 2:
                    return None
                a, b = norm_w(ss.replace(' ', '')), norm_w(gs.replace(' ', ''))
                if difflib.SequenceMatcher(None, a, b).ratio() < 0.5:
                    return None
                s_sc = max((roster_score(x, roster) for x in ss.split()), default=0.0)
                g_sc = max((roster_score(x, roster) for x in gs.split()), default=0.0)
                if s_sc > g_sc + 0.02:
                    return 'soniox'
                if g_sc > s_sc + 0.02:
                    return 'gemini'
                return None
            calls = [roster_call(ss, gs) for ss, gs in diffs]
            if all(calls) and len(set(calls)) == 1:
                verdict = calls[0]
                who = 'Soniox' if verdict == 'soniox' else 'Gemini'
                notes.append(f'Roster resolves every diff: {who} right.')
            else:
                verdict = 'judge'
                one_sided = {c for c in calls if c}
                if len(one_sided) == 1:
                    who = 'Soniox' if one_sided == {'soniox'} else 'Gemini'
                    notes.append(f'(hint: roster-decidable diffs side with {who}; the rest need your ear)')
                notes.append('Please judge: who heard it right?')
        if g_lat is not None:
            faster = 'Soniox' if s_lat <= g_lat else 'Gemini'
            notes.append(f'Fastest: {faster} ({min(s_lat, g_lat):.1f}s vs {max(s_lat, g_lat):.1f}s).')
        note = ' '.join(notes)

        # re-apply saved reviews: latest verdict tag wins; note = latest comment that a
        # human actually wrote (differs from the auto note a click would echo back)
        for sv in saved.get(round(t_start, 1), []):
            if 'verdict_gemini' in sv['tags']:
                verdict = 'gemini'
            elif 'verdict_soniox' in sv['tags']:
                verdict = 'soniox'
        human = [sv['comment'] for sv in saved.get(round(t_start, 1), [])
                 if sv['comment'] and sv['comment'] != note]
        if human:
            note = human[-1]
        rows.append({'t': t_start, 'stx': s_text, 'slat': s_lat,
                     'gtx': g_text, 'glat': g_lat, 'note': note,
                     'verdict': verdict, 'was_judge': not g_idx or s_norm != g_norm})

    def mmss(t):
        return f"{int(t // 60)}:{t % 60:04.1f}"

    roster_list = roster_display()
    params_html = f"""<details style='background:#101826;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:10px'>
<summary style='cursor:pointer'><b>Test setup — roster + engine params</b> (click to unfold)</summary>
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
    s_fast = sum(1 for r in both if r['slat'] <= r['glat'])
    n_judge = sum(1 for r in rows if r['verdict'] == 'judge')
    cls_map = {'soniox': 'win-s', 'gemini': 'win-g', 'judge': 'judge'}
    trs = ''
    for i, r in enumerate(rows):
        gl = f"{r['glat']:.1f}s" if r['glat'] is not None else '—'
        btns = (f"<div class=vbs><span class=vb data-i={i} data-v=soniox>Soniox ✓</span>"
                f"<span class=vb data-i={i} data-v=gemini>Gemini ✓</span></div>") if r['was_judge'] else ''
        trs += f"""<tr><td><a href='#' onclick="seek({r['t']:.1f});return false">{mmss(r['t'])}</a></td>
<td>{html.escape(r['stx'])}</td><td class=lat>{r['slat']:.1f}s</td>
<td>{html.escape(r['gtx']) or '<i>—</i>'}</td><td class=lat>{gl}</td>
<td class="note {cls_map[r['verdict']]}"><span class=ntext contenteditable data-i={i}>{html.escape(r['note'])}</span>{btns}</td></tr>\n"""

    page = f"""<meta charset=utf-8><title>ASR compare — {CLIPID}</title>
<style>body{{background:#0a0a0a;color:#ddd;font:13px system-ui;margin:18px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #333;padding:5px;vertical-align:top;font-size:12.5px}}
th{{background:#161616}}video{{width:640px;display:block;margin:8px 0;position:sticky;top:0;z-index:5;background:#000}}
a{{color:#7dd3fc}}.lat{{white-space:nowrap;text-align:right;color:#a3e635}}
.note{{min-width:240px}}.ntext{{display:block}}.ntext:focus{{outline:1px solid #3b82f6;background:#111827}}
.note.win-s{{background:#2a1212;color:#fca5a5}}.note.win-g{{background:#122a16;color:#86efac}}
.note.judge{{background:#2a2110;color:#fbbf24}}
.vbs{{margin-top:4px}}.vb{{display:inline-block;border:1px solid #334155;border-radius:9px;padding:1px 8px;margin-right:4px;cursor:pointer;font-size:11px;color:#94a3b8}}
.vb.on{{background:#1e3a5f;color:#dbeafe;border-color:#3b82f6}}
#bar{{position:fixed;bottom:0;left:0;right:0;background:#0d1420;border-top:1px solid #1e3a5f;padding:8px;text-align:center}}
#bar button{{background:#1e3a5f;color:#dbeafe;border:0;border-radius:4px;padding:6px 16px;cursor:pointer}}</style>
<h2>ASR compare — Soniox v5 vs Gemini 3.5 Transcribe Live ({TAG}) — {CLIPID}</h2>
<p>Both engines streamed real-time with the full roster as biasing context. Latency = seconds from
audio end of the sentence to that engine's final transcript.
Notes: <span style='background:#122a16;color:#86efac;padding:1px 6px;border-radius:4px'>green = Gemini wins</span>
<span style='background:#2a1212;color:#fca5a5;padding:1px 6px;border-radius:4px'>red = Soniox wins</span>
<span style='background:#2a2110;color:#fbbf24;padding:1px 6px;border-radius:4px'>orange = transcripts differ — your call</span>.
On orange rows: click a time to listen, hit <b>Soniox ✓</b> / <b>Gemini ✓</b> — the row recolors and the verdict
<b>auto-saves instantly</b> (watch for "verdict auto-saved ✓" bottom right). Note-text edits still need <b>Save</b>. {len(rows)} sentences, {n_judge} need your verdict; where both produced output,
Soniox was faster on {s_fast}/{len(both)}.</p>
{params_html}
<video id=v controls preload=metadata src="{VIDEO_SRC}"></video>
<p><a href="{VIDEO_SRC}" download="{CLIPID}.mp4">⬇ Download video</a></p>
<table><tr><th>start</th><th>Soniox v5</th><th>final</th><th>Gemini live</th><th>final</th><th>notes</th></tr>
{trs}</table>
<div id=bar><span id=cnt>0 changes</span> <button onclick="submitN()">Save verdicts + notes</button> <span id=st></span></div>
<script>
const V={{}},ORIG={{}};
document.querySelectorAll('.ntext').forEach(n=>{{ORIG[n.dataset.i]=n.textContent;
 n.addEventListener('input',updateCnt);}});
function changed(){{
 const edited=[...document.querySelectorAll('.ntext')].filter(x=>x.textContent!==ORIG[x.dataset.i]).map(x=>x.dataset.i);
 return new Set([...edited,...Object.keys(V)]);}}
function updateCnt(){{document.getElementById('cnt').textContent=changed().size+' changes';}}
function seek(t){{const v=document.getElementById('v');v.currentTime=Math.max(0,t-1.0);v.play();}}
function itemFor(i){{
 const n=document.querySelector(`.ntext[data-i='${{i}}']`),r=n.closest('tr');
 return {{t:parseFloat(r.cells[0].innerText.split(':').reduce((m,s)=>60*m+ +s,0)),col:0,column:'ASR',
  profile:'asr',clip:'{CLIPID}',cell_text:(r.cells[1].innerText+' || '+r.cells[3].innerText).slice(0,250),
  tags:[V[i]?'verdict_'+V[i]:'note_edit'],comment:n.textContent.slice(0,500)}};}}
function post(items,ok){{
 return fetch('/blend_feedback',{{method:'POST',body:JSON.stringify({{reviewer:'ben',version:'asrcompare',items:items}})}})
  .then(r=>r.json()).then(j=>document.getElementById('st').textContent=j.stored?' '+ok:' save error: '+j.error)
  .catch(()=>document.getElementById('st').textContent=' network error — click NOT saved');}}
document.querySelectorAll('.vb').forEach(b=>b.addEventListener('click',()=>{{
 const i=b.dataset.i,v=b.dataset.v;V[i]=v;
 const cell=b.closest('.note');cell.classList.remove('judge','win-s','win-g');
 cell.classList.add(v==='gemini'?'win-g':'win-s');
 cell.querySelectorAll('.vb').forEach(x=>x.classList.toggle('on',x===b));updateCnt();
 post([itemFor(i)],'verdict auto-saved ✓');}}));
function submitN(){{
 const items=[...changed()].map(itemFor);
 if(!items.length){{document.getElementById('st').textContent=' nothing to save';return;}}
 post(items,'saved '+items.length+' ✓');}}
</script>"""
    out = Path(f'/var/www/html/experiments/asr_compare/{CLIPID}')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(page)
    counts = {v: sum(1 for r in rows if r['verdict'] == v) for v in ('soniox', 'gemini', 'judge')}
    print(f"rows={len(rows)} verdicts={counts} soniox_faster={s_fast}/{len(both)} "
          f"saved_applied={len(saved)} -> {out}/index.html")


if __name__ == '__main__':
    main()
