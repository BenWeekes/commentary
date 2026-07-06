#!/usr/bin/env python3
import json, html
from pathlib import Path
from datetime import datetime, timezone

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
rows_all = [json.loads(l) for l in open(BASE / 'commentary_v3.jsonl')]
rows_played = [json.loads(l) for l in open(BASE / 'commentary_v3_scheduled.jsonl')]
OUT = BASE / 'ai_commentator_v3_results.html'

played_lats = sorted(int(r['realistic_lag_s']*1000) for r in rows_played)
vision_lats = sorted(int(r['vision_latency_ms']) for r in rows_played)
tts_lats = sorted(int(r['tts_ms']) for r in rows_played)
def pct(arr, p): return arr[int(len(arr)*p)] if arr else 0
TOTAL_BURSTS = 542
SKIPPED = TOTAL_BURSTS - len(rows_all)

def fmt_ts(s):
    s = float(s); return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"

transcript_html = []
for r in rows_played:
    transcript_html.append(
        f"<tr data-start='{r['scheduled_start_s']:.3f}'>"
        f"<td class='ts'>{fmt_ts(r['video_time_s'])}</td>"
        f"<td class='ts'>{fmt_ts(r['scheduled_start_s'])}</td>"
        f"<td class='lag'>+{int(r['realistic_lag_s']*1000)}ms</td>"
        f"<td class='text'>{html.escape(r['text'])}</td></tr>"
    )

doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AI commentator v3 — calibrated naming, live pacing</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2em; max-width: 1100px; line-height: 1.5; color: #222; }}
  h1, h2 {{ font-weight: 600; }}
  h2 {{ margin-top: 2em; border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }}
  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1em; margin: 1em 0; }}
  .stat {{ background: #f5f5f7; padding: 1em; border-radius: 8px; }}
  .stat .v {{ font-size: 1.6em; font-weight: 600; color: #0066cc; }}
  .stat .k {{ font-size: 0.85em; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
  video {{ width: 100%; max-width: 800px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.92em; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 0.4em 0.7em; text-align: left; }}
  th {{ background: #f9f9f9; }}
  td.ts {{ font-family: monospace; white-space: nowrap; color: #666; }}
  td.lag {{ font-family: monospace; color: #c70; }}
  td.text {{ width: 60%; }}
  .ab {{ background: #eef6ff; padding: 1em; border-left: 4px solid #0066cc; margin: 1em 0; }}
</style></head><body>

<h1>AI commentator v3 — calibrated naming + live pacing</h1>
<p>Same slice: <code>m05_uni_eval_25min</code> minutes 5:00–10:00, 562 gold words. Same vision model (gpt-5.4-mini). Same TTS (ElevenLabs).</p>

<div class="ab"><b>Compare:</b>
<a href="/ai_commentator_results.html">v1 (dense, name-heavy, Becker-after-sub problem)</a> ·
<a href="/ai_commentator_v2_results.html">v2 (live-pace, strict naming, name-shy)</a> ·
<b>v3 (this page — calibrated)</b>
</div>

<h2>What's different in v3</h2>
<ul>
  <li>Kept v2's <b>live-pace gate</b> (vision only fires when previous TTS would be done)</li>
  <li>Kept v2's <b>fallback vocabulary</b> (pass type, tactical, time, atmosphere)</li>
  <li>Kept v2's <b>pre-game player insights</b> from gold (Burke / Sieb / Doekhi / Weiper / Kohn / Zentner / Veratschnig / Juranovic / Posch)</li>
  <li><b>NAMING — LEAN INTO IT</b>: explicit "name players whenever reasonable, occasional misidentifications are acceptable, a wrong name is far less damaging than every line being generic". Reverses v2's name-shy behaviour.</li>
  <li>Slightly higher token budget (50 vs 40) to allow brief insight asides.</li>
</ul>

<h2>Listen</h2>
<video controls preload="metadata" src="/ai_commentary_v3.mp4"></video>
<p><a href="/ai_commentary_v3_sidebyside.mp4">side-by-side (original L / AI R)</a> · <a href="/v2v_source_5min.mp4">original audio only</a></p>

<h2>Latency &amp; sync</h2>
<div class="stats">
  <div class="stat"><div class="k">Vision p50 / p90</div><div class="v">{pct(vision_lats,0.5)} / {pct(vision_lats,0.9)} ms</div></div>
  <div class="stat"><div class="k">TTS p50 / p90</div><div class="v">{pct(tts_lats,0.5)} / {pct(tts_lats,0.9)} ms</div></div>
  <div class="stat"><div class="k">End-to-end p50 / p90</div><div class="v">{pct(played_lats,0.5)} / {pct(played_lats,0.9)} ms</div></div>
</div>

<h2>Coverage</h2>
<div class="stats">
  <div class="stat"><div class="k">Total bursts</div><div class="v">{TOTAL_BURSTS}</div></div>
  <div class="stat"><div class="k">Vision calls</div><div class="v">{len(rows_all)}</div></div>
  <div class="stat"><div class="k">Clips played</div><div class="v">{len(rows_played)}</div></div>
</div>

<h2>Played transcript</h2>
<table><thead><tr><th>video time</th><th>played at</th><th>lag</th><th>commentary</th></tr></thead>
<tbody>{''.join(transcript_html)}</tbody></table>

<script>
  document.querySelectorAll('tr[data-start]').forEach(row => {{
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => {{
      const t = parseFloat(row.dataset.start);
      const v = document.querySelector('video'); v.currentTime = t; v.play();
    }});
  }});
</script>
<p style="margin-top: 3em; color: #666; font-size: 0.85em;">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
</body></html>
"""

OUT.write_text(doc)
print(f"Wrote {OUT} ({len(doc)/1024:.0f} KB)")
print(f"Played: {len(rows_played)}, vision calls: {len(rows_all)}")
