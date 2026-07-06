#!/usr/bin/env python3
"""Build v2 results page (with link back to v1 for A/B)."""
import json, html
from pathlib import Path
from datetime import datetime, timezone

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
COMMENTARY_ALL = BASE / 'commentary_v2.jsonl'
COMMENTARY_PLAYED = BASE / 'commentary_v2_scheduled.jsonl'
OUT = BASE / 'ai_commentator_v2_results.html'

rows_all = [json.loads(l) for l in open(COMMENTARY_ALL)]
rows_played = [json.loads(l) for l in open(COMMENTARY_PLAYED)]

played_lats = sorted(int(r['realistic_lag_s']*1000) for r in rows_played)
vision_lats = sorted(int(r['vision_latency_ms']) for r in rows_played)
tts_lats = sorted(int(r['tts_ms']) for r in rows_played)
def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0

dropped_reasons = {}
for r in rows_all:
    if not r['accepted']:
        dropped_reasons[r['reason']] = dropped_reasons.get(r['reason'], 0) + 1

# Skipped (booth busy) count = total bursts - vision calls made
TOTAL_BURSTS = 542
VISION_CALLS = len(rows_all)
SKIPPED = TOTAL_BURSTS - VISION_CALLS

def fmt_ts(s):
    s = float(s)
    return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"

transcript_html = []
for r in rows_played:
    vt = float(r['video_time_s']); pa = float(r['scheduled_start_s'])
    transcript_html.append(
        f"<tr data-start='{pa:.3f}'>"
        f"<td class='ts'>{fmt_ts(vt)}</td>"
        f"<td class='ts'>{fmt_ts(pa)}</td>"
        f"<td class='lag'>+{int(r['realistic_lag_s']*1000)}ms</td>"
        f"<td class='text'>{html.escape(r['text'])}</td>"
        f"</tr>"
    )

ALL_TABLE = []
for r in rows_all:
    status = '✓ played' if r['accepted'] else (r['reason'] or 'unknown')
    ALL_TABLE.append(
        f"<tr class='r-{status.split()[0]}'>"
        f"<td class='ts'>{fmt_ts(r['video_time_s'])}</td>"
        f"<td>{int(r['vision_latency_ms']) if r['vision_latency_ms'] else '-'}ms</td>"
        f"<td>{status}</td>"
        f"<td class='text'>{html.escape(r.get('text') or '')}</td>"
        f"</tr>"
    )

html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>AI commentator v2 — strict naming + live pacing</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2em; max-width: 1100px; line-height: 1.5; color: #222; }}
  h1, h2 {{ font-weight: 600; color: #111; }}
  h2 {{ margin-top: 2em; border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }}
  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1em; margin: 1em 0; }}
  .stat {{ background: #f5f5f7; padding: 1em; border-radius: 8px; }}
  .stat .v {{ font-size: 1.6em; font-weight: 600; color: #0066cc; }}
  .stat .k {{ font-size: 0.85em; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
  video {{ width: 100%; max-width: 800px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.92em; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 0.4em 0.7em; text-align: left; vertical-align: top; }}
  th {{ background: #f9f9f9; }}
  td.ts {{ font-family: monospace; white-space: nowrap; color: #666; }}
  td.lag {{ font-family: monospace; color: #c70; white-space: nowrap; }}
  td.text {{ width: 60%; }}
  tr.r-no_call td {{ color: #aaa; font-style: italic; }}
  tr.r-repetitive td {{ color: #bbb; font-style: italic; }}
  tr.r-error td {{ color: #c33; }}
  details {{ margin-top: 1em; }}
  summary {{ cursor: pointer; font-weight: 600; }}
  .note {{ background: #fff8e6; padding: 1em; border-left: 4px solid #f0b400; margin: 1em 0; }}
  .ab {{ background: #eef6ff; padding: 1em; border-left: 4px solid #0066cc; margin: 1em 0; }}
</style></head><body>

<h1>AI commentator v2 — strict naming + live pacing</h1>

<p>Source: <code>clips/m05_uni_eval_25min/source.mp4</code>, window 5:00&ndash;10:00. Same slice as v1 so they're directly comparable.</p>

<div class="ab"><b>A/B vs v1:</b>
<a href="/ai_commentator_results.html">▸ v1 results page (dense pacing, soft naming rule)</a>
&nbsp;|&nbsp;
<a href="/ai_commentary.mp4">v1 MP4</a>
</div>

<h2>What changed in v2</h2>
<ul>
  <li><b>Strict naming rule</b> — model only names a player if shirt number is legible AND matches roster; otherwise role description. Explicitly told "do not carry forward a name from earlier frames — the player may have been substituted." Targets the v1 Becker-after-sub problem.</li>
  <li><b>Fallback vocabulary</b> — pass type, tempo, tactical read, time reference, atmosphere, terse outcome. Gives the model more to say when no clear named player is identifiable.</li>
  <li><b>13 style examples from the actual broadcast booth</b> — pulled from gold STT for THIS match. Pure style, no spoilers.</li>
  <li><b>Per-player pre-game insights</b> — 9 players carry biographical context drawn directly from this broadcast's own commentary (form, on-loan status, country, manager preferences). No invented facts.</li>
  <li><b>Live-pace simulation</b> — vision calls are gated on a single "booth busy" timer (only call when previous TTS would have finished). Same as a live commentary booth that can only speak one mouth at a time. No parallel batch.</li>
  <li><b>Tighter dedup</b> (Jaccard 0.4 vs v1's 0.7).</li>
  <li><b>Shorter utterance target</b> (3-10 words vs v1's 4-16).</li>
</ul>

<h2>Listen</h2>
<video controls preload="metadata" src="/ai_commentary_v2.mp4"></video>
<p style="margin-top: 0.5em; font-size: 0.9em; color: #666;">
  <a href="/ai_commentary_v2_sidebyside.mp4">side-by-side (original audio left, AI commentary right)</a> &nbsp;|&nbsp;
  <a href="/v2v_source_5min.mp4">original English audio only</a>
</p>

<h2>Latency &amp; sync</h2>
<div class="stats">
  <div class="stat"><div class="k">Vision p50 / p90</div><div class="v">{pct(vision_lats,0.5)} / {pct(vision_lats,0.9)} ms</div></div>
  <div class="stat"><div class="k">TTS p50 / p90</div><div class="v">{pct(tts_lats,0.5)} / {pct(tts_lats,0.9)} ms</div></div>
  <div class="stat"><div class="k">End-to-end p50 / p90</div><div class="v">{pct(played_lats,0.5)} / {pct(played_lats,0.9)} ms</div></div>
</div>
<p>Sync: each clip's audio is scheduled at <code>video_time + vision_latency + tts_latency</code>. Booth is gated so a new vision call only fires after the previous TTS would have finished &mdash; no overlap, no backlog.</p>

<h2>Coverage</h2>
<div class="stats">
  <div class="stat"><div class="k">Total bursts</div><div class="v">{TOTAL_BURSTS}</div></div>
  <div class="stat"><div class="k">Vision calls made</div><div class="v">{VISION_CALLS}</div><div class="k" style="margin-top:0.5em">{SKIPPED} skipped (booth busy)</div></div>
  <div class="stat"><div class="k">Clips actually played</div><div class="v">{len(rows_played)}</div></div>
</div>
<p>Rejected by model: {dropped_reasons}.</p>

<h2>Played transcript (STT-style)</h2>
<table>
  <thead><tr><th>video time</th><th>played at</th><th>lag</th><th>commentary</th></tr></thead>
  <tbody>{''.join(transcript_html)}</tbody>
</table>

<details>
  <summary>All {VISION_CALLS} vision calls (played / no_call / repetitive)</summary>
  <table>
    <thead><tr><th>video time</th><th>vision latency</th><th>status</th><th>text</th></tr></thead>
    <tbody>{''.join(ALL_TABLE)}</tbody>
  </table>
</details>

<h2>What was passed to the vision model (v2)</h2>
<ul>
  <li><b>Per burst</b>: 4 JPEG frames (960&times;540, q=72)</li>
  <li><b>Match block</b>: title, venue, teams + formations + kit colours, storyline, Union manager profile (Marie-Louise Eta, first woman head coach in Bundesliga history), Union ex-manager context (Urs Fischer)</li>
  <li><b>Squad (40 players)</b>: <code>#N ShortName (FullName) [role/position]</code> with per-player <b>pre-game insight</b> for 9 players where the real broadcast carried context: Burke, Sieb, Doekhi, Weiper, Kohn, Zentner, Veratschnig, Juranovic, Posch.</li>
  <li><b>13 style example lines</b> from this broadcast's own gold STT</li>
  <li><b>Strict naming rule</b>: name only when shirt number is legible AND matches roster; never carry forward a name from earlier frames; default to role</li>
  <li><b>Fallback vocabulary</b>: pass type, tactical read, time, atmosphere, terse outcome</li>
  <li><b>Last 6 accepted lines</b> for dedup</li>
</ul>

<p style="margin-top: 3em; color: #666; font-size: 0.85em;">
  Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.
  Source: <code>experiments/ai_commentator/</code>
</p>

<script>
  document.querySelectorAll('tr[data-start]').forEach(row => {{
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => {{
      const t = parseFloat(row.dataset.start);
      const v = document.querySelector('video');
      v.currentTime = t; v.play();
    }});
  }});
</script>

</body></html>
"""

OUT.write_text(html_doc)
print(f"Wrote {OUT}  ({len(html_doc)/1024:.0f} KB)")
print(f"Played: {len(rows_played)}, vision calls: {VISION_CALLS}, skipped: {SKIPPED}")
