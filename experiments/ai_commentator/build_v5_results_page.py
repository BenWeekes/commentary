#!/usr/bin/env python3
"""v5 results page: 4 transcript columns (Soniox | v5 EN | Gemini EN | v5 FR), audio toggle includes Gemini tracks."""
import json, html
from pathlib import Path
from datetime import datetime, timezone

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
OUT = BASE / 'v5.html'

gold = [json.loads(l) for l in open(BASE / 'gold_soniox_5min.jsonl')]
v5_en = [json.loads(l) for l in open(BASE / 'commentary_v5_en_tagged.jsonl')]
v5_fr = [json.loads(l) for l in open(BASE / 'commentary_v5_fr_tagged.jsonl')]
gem_en = [json.loads(l) for l in open(BASE / 'commentary_gemini_en_tagged.jsonl')]


def fmt_ts(s):
    s = float(s)
    return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"


def gold_rows(items):
    out = []
    for r in items:
        spk = r.get('speaker', 0)
        out.append(
            f"<div class='line' data-start='{r['start_s']:.3f}'>"
            f"<span class='ts'>{fmt_ts(r['start_s'])}</span>"
            f"<span class='spk spk-{spk}'>S{spk}</span>"
            f"<span class='text'>{html.escape(r['text'])}</span>"
            f"</div>"
        )
    return ''.join(out)


def ai_rows(items, lang_key='text'):
    out = []
    for r in items:
        text = r.get(lang_key) or r.get('text') or ''
        tag = r.get('tag', '')
        ts = r.get('natural_start_s', r.get('scheduled_start_s', r.get('video_time_s', 0)))
        tag_html = f"<span class='tag'>{html.escape(tag)}</span> " if tag else ''
        sub_marker = ''
        if r.get('sub_detected'):
            off, on = r['sub_detected']
            sub_marker = f"<span class='sub'>↻ {html.escape(off)}↔{html.escape(on)}</span> "
        out.append(
            f"<div class='line' data-start='{ts:.3f}'>"
            f"<span class='ts'>{fmt_ts(ts)}</span>"
            f"<span class='text'>{tag_html}{sub_marker}{html.escape(text)}</span>"
            f"</div>"
        )
    return ''.join(out)


doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>AI commentator v5 — Soniox vs v5 vs Gemini</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#0a0a0a; color:#e0e0e0; min-height:100vh; }}
.wrap {{ max-width: 1600px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 22px; color:#fff; letter-spacing:-0.5px; margin-bottom:6px; }}
p.sub {{ color:#888; font-size:13px; margin-bottom:18px; line-height:1.5; }}
p.sub a {{ color:#60a5fa; text-decoration:none; }}
p.sub a:hover {{ text-decoration:underline; }}
.note {{ background:#1a2030; border-left:3px solid #4ade80; padding:10px 14px; border-radius:4px;
         font-size:13px; color:#cbd5e1; margin: 14px 0 18px; line-height:1.55; }}
.note b {{ color:#4ade80; }}
.video-row {{ background:#111; border:1px solid #1f1f1f; border-radius:8px; padding:16px; margin-bottom:18px; }}
.video-row video {{ width:100%; max-width:880px; display:block; margin:0 auto 12px; background:#000; border-radius:4px; }}
.audio-tabs {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:center; }}
.audio-tabs button {{ background:#1a1a1a; color:#aaa; border:1px solid #2a2a2a; padding:8px 14px;
                      font-size:12px; border-radius:18px; cursor:pointer; font-weight:600;
                      transition: all .15s; }}
.audio-tabs button:hover {{ border-color:#3a3a3a; color:#eee; }}
.audio-tabs button.active {{ background:#052e16; color:#4ade80; border-color:#166534; }}
.cols {{ display:grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap:10px; }}
.col {{ background:#111; border:1px solid #1f1f1f; border-radius:8px; overflow:hidden; }}
.col-head {{ padding: 10px 14px; border-bottom:1px solid #1f1f1f; font-size:11px; font-weight:700;
             text-transform:uppercase; letter-spacing:0.5px; color:#aaa; display:flex; align-items:center;
             justify-content:space-between; }}
.col-head .count {{ color:#555; font-weight:500; }}
.col-soniox .col-head {{ color:#fbbf24; }}
.col-v5-en .col-head {{ color:#60a5fa; }}
.col-gemini-en .col-head {{ color:#34d399; }}
.col-v5-fr .col-head {{ color:#f472b6; }}
.lines {{ max-height: 70vh; overflow-y:auto; padding:6px 0; }}
.line {{ padding: 7px 12px; cursor:pointer; border-left: 3px solid transparent;
         font-size: 12.5px; line-height: 1.45; transition: background .1s; }}
.line:hover {{ background:#1a1a1a; }}
.line.active {{ background:#1a2030; border-left-color:#4ade80; }}
.line .ts {{ display:inline-block; min-width:58px; font-family: ui-monospace, monospace;
             font-size:10.5px; color:#555; margin-right:6px; }}
.line .spk {{ display:inline-block; font-size:9.5px; font-weight:700; padding:1px 4px; border-radius:3px;
              margin-right:6px; }}
.spk-0 {{ background:#1f2937; color:#a78bfa; }}
.spk-1 {{ background:#2d2400; color:#fbbf24; }}
.line .tag {{ font-size:9.5px; font-weight:700; color:#a78bfa; padding:1px 4px;
              background:#1a1a1a; border-radius:3px; margin-right:4px; }}
.line .sub {{ font-size:9.5px; font-weight:700; color:#fbbf24; padding:1px 4px;
              background:#2d2400; border-radius:3px; margin-right:4px; }}
.line .text {{ color:#cbd5e1; }}
.legend {{ display:flex; gap:14px; font-size:11px; color:#666; margin: 8px 0 16px; flex-wrap:wrap; }}
.legend .swatch {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }}
.summary-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 14px 0; }}
.summary-card {{ background:#111; border:1px solid #1f1f1f; border-radius:6px; padding:10px 12px; }}
.summary-card .k {{ font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.5px; }}
.summary-card .v {{ font-size:18px; font-weight:600; color:#cbd5e1; margin-top:2px; }}
</style></head><body>

<div class="wrap">
  <h1>AI commentator v5 + Gemini comparison</h1>
  <p class="sub">Source slice: <code>m05_uni_eval_25min</code>, minutes 5:00–10:00 (Mainz 05 vs Union Berlin, Bundesliga MD33).
   Click any line in any column to seek the video.
   <a href="/experiments/ai_commentator/v4.html">v4 comparison</a> ·
   <a href="/experiments/ai_commentator/">all experiments</a>
  </p>

  <div class="summary-grid">
    <div class="summary-card"><div class="k">Soniox (real broadcast)</div><div class="v">{len(gold)} turns</div></div>
    <div class="summary-card"><div class="k">v5 EN (gpt-5.4-mini)</div><div class="v">{len(v5_en)} lines</div></div>
    <div class="summary-card"><div class="k">Gemini EN (2.5 flash)</div><div class="v">{len(gem_en)} lines</div></div>
    <div class="summary-card"><div class="k">v5 FR</div><div class="v">{len(v5_fr)} lines</div></div>
    <div class="summary-card"><div class="k">Target cadence</div><div class="v">~45 / 5 min</div></div>
  </div>

  <div class="note">
   <b>v5 changes from v4 (all generic, pre-game info only):</b>
   (1) <b>sub-event memory</b> — once a sub is announced, future calls are told it happened and won't re-announce
   (2) <b>trigram dedup</b> — catches near-duplicate phrasings ("Amiri over it" / "Amiri over this dead ball")
   (3) <b>frame carry-over</b> — first frame of each burst is the last frame of the previous, giving the model visual continuity
   (4) <b>dynamic booth-busy gate</b> — longer pause after [calm]/[flatly] lines, shorter after action
   (5) <b>stronger NO_CALL</b> — explicit "NO_CALL is right 40-50% of the time"
   <br><br>
   <b>Gemini column</b>: same v5 pipeline, but vision call swapped to <code>gemini-2.5-flash</code>. Different model, same prompt, same context, same dedup. Direct A/B for vision-model quality.
  </div>

  <div class="video-row">
    <video id="player" controls preload="metadata"
           src="/experiments/ai_commentator/v5_brit_synced.mp4"></video>
    <div class="audio-tabs">
      <button data-src="/experiments/ai_commentator/original_with_human_commentary.mp4">Original broadcast</button>
      <button data-src="/experiments/ai_commentator/v5_brit_synced.mp4" class="active">v5 EN (gpt-5.4-mini)</button>
      <button data-src="/experiments/ai_commentator/v5_fr_synced.mp4">v5 FR</button>
      <button data-src="/experiments/ai_commentator/gemini_brit_synced.mp4">Gemini EN (2.5 flash)</button>
      <button data-src="/experiments/ai_commentator/gemini_fr_synced.mp4">Gemini FR</button>
      <button data-src="/experiments/ai_commentator/v4_brit_synced.mp4">v4 EN (compare)</button>
    </div>
  </div>

  <div class="legend">
    <span><span class="swatch" style="background:#fbbf24"></span>Soniox gold STT (real broadcast)</span>
    <span><span class="swatch" style="background:#60a5fa"></span>v5 — gpt-5.4-mini</span>
    <span><span class="swatch" style="background:#34d399"></span>Gemini — 2.5 flash</span>
    <span><span class="swatch" style="background:#f472b6"></span>v5 French</span>
    <span style="margin-left:auto"><span class="sub" style="background:#2d2400;color:#fbbf24;padding:1px 4px;border-radius:3px;">↻ sub</span> = detected substitution</span>
  </div>

  <div class="cols">
    <div class="col col-soniox">
      <div class="col-head"><span>Soniox gold STT</span><span class="count">{len(gold)} turns</span></div>
      <div class="lines">{gold_rows(gold)}</div>
    </div>
    <div class="col col-v5-en">
      <div class="col-head"><span>AI EN — v5 (gpt-5.4-mini)</span><span class="count">{len(v5_en)} lines</span></div>
      <div class="lines">{ai_rows(v5_en, 'text')}</div>
    </div>
    <div class="col col-gemini-en">
      <div class="col-head"><span>AI EN — Gemini 2.5 flash</span><span class="count">{len(gem_en)} lines</span></div>
      <div class="lines">{ai_rows(gem_en, 'text')}</div>
    </div>
    <div class="col col-v5-fr">
      <div class="col-head"><span>AI FR — v5</span><span class="count">{len(v5_fr)} lines</span></div>
      <div class="lines">{ai_rows(v5_fr, 'fr')}</div>
    </div>
  </div>
</div>

<script>
  const player = document.getElementById('player');
  document.querySelectorAll('.audio-tabs button').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.audio-tabs button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const t = player.currentTime;
      const wasPlaying = !player.paused;
      player.src = btn.dataset.src;
      player.addEventListener('loadedmetadata', () => {{
        player.currentTime = t;
        if (wasPlaying) player.play();
      }}, {{ once: true }});
    }});
  }});

  document.querySelectorAll('.line').forEach(line => {{
    line.addEventListener('click', () => {{
      const t = parseFloat(line.dataset.start);
      player.currentTime = Math.max(0, t - 0.2);
      player.play();
    }});
  }});

  function highlight() {{
    const t = player.currentTime;
    document.querySelectorAll('.col').forEach(col => {{
      const lines = col.querySelectorAll('.line');
      let active = null;
      for (const l of lines) {{
        const start = parseFloat(l.dataset.start);
        if (start <= t + 0.05) active = l;
        l.classList.remove('active');
      }}
      if (active) active.classList.add('active');
    }});
  }}
  player.addEventListener('timeupdate', highlight);
</script>
</body></html>
"""

OUT.write_text(doc)
print(f"Wrote {OUT} ({len(doc)/1024:.0f} KB)")
print(f"  gold: {len(gold)}  v5_en: {len(v5_en)}  gemini_en: {len(gem_en)}  v5_fr: {len(v5_fr)}")
