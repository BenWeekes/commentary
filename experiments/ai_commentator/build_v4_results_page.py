#!/usr/bin/env python3
"""Build the v4 3-column comparison UI (Soniox EN | AI EN | AI FR), synced video."""
import json, html
from pathlib import Path
from datetime import datetime, timezone

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
OUT = BASE / 'v4.html'

gold = [json.loads(l) for l in open(BASE / 'gold_soniox_5min.jsonl')]
ai_en = [json.loads(l) for l in open(BASE / 'commentary_v4_en_tagged.jsonl')]
ai_fr = [json.loads(l) for l in open(BASE / 'commentary_v4_fr_tagged.jsonl')]


def fmt_ts(s):
    s = float(s)
    return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"


def gold_rows(items):
    out = []
    for r in items:
        spk = r.get('speaker', 0)
        spk_label = f"S{spk}"
        out.append(
            f"<div class='line' data-start='{r['start_s']:.3f}'>"
            f"<span class='ts'>{fmt_ts(r['start_s'])}</span>"
            f"<span class='spk spk-{spk}'>{spk_label}</span>"
            f"<span class='text'>{html.escape(r['text'])}</span>"
            f"</div>"
        )
    return ''.join(out)


def ai_rows(items, lang_key='text', show_tag=True):
    out = []
    for r in items:
        text = r.get(lang_key) or r.get('text') or ''
        tag = r.get('tag', '')
        ts = r.get('natural_start_s', r.get('fr_start_s', r.get('scheduled_start_s', r.get('video_time_s', 0))))
        tag_html = f"<span class='tag'>{html.escape(tag)}</span> " if show_tag and tag else ''
        out.append(
            f"<div class='line' data-start='{ts:.3f}'>"
            f"<span class='ts'>{fmt_ts(ts)}</span>"
            f"<span class='text'>{tag_html}{html.escape(text)}</span>"
            f"</div>"
        )
    return ''.join(out)


doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>AI commentator v4 — Soniox vs AI EN vs AI FR</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#0a0a0a; color:#e0e0e0; min-height:100vh; }}
.wrap {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
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
.audio-tabs button {{ background:#1a1a1a; color:#aaa; border:1px solid #2a2a2a; padding:8px 18px;
                      font-size:13px; border-radius:18px; cursor:pointer; font-weight:600;
                      transition: all .15s; }}
.audio-tabs button:hover {{ border-color:#3a3a3a; color:#eee; }}
.audio-tabs button.active {{ background:#052e16; color:#4ade80; border-color:#166534; }}
.cols {{ display:grid; grid-template-columns: 1fr 1fr 1fr; gap:14px; }}
.col {{ background:#111; border:1px solid #1f1f1f; border-radius:8px; overflow:hidden; }}
.col-head {{ padding: 10px 14px; border-bottom:1px solid #1f1f1f; font-size:12px; font-weight:700;
             text-transform:uppercase; letter-spacing:0.5px; color:#aaa; display:flex; align-items:center;
             justify-content:space-between; }}
.col-head .count {{ color:#555; font-weight:500; }}
.col-soniox .col-head {{ color:#fbbf24; }}
.col-ai-en .col-head {{ color:#60a5fa; }}
.col-ai-fr .col-head {{ color:#f472b6; }}
.lines {{ max-height: 70vh; overflow-y:auto; padding:6px 0; }}
.line {{ padding: 8px 14px; cursor:pointer; border-left: 3px solid transparent;
         font-size: 13px; line-height: 1.5; transition: background .1s; }}
.line:hover {{ background:#1a1a1a; }}
.line.active {{ background:#1a2030; border-left-color:#4ade80; }}
.line .ts {{ display:inline-block; min-width:62px; font-family: ui-monospace, monospace;
             font-size:11px; color:#555; margin-right:8px; }}
.line .spk {{ display:inline-block; font-size:10px; font-weight:700; padding:1px 5px; border-radius:3px;
              margin-right:6px; }}
.spk-0 {{ background:#1f2937; color:#a78bfa; }}
.spk-1 {{ background:#2d2400; color:#fbbf24; }}
.line .tag {{ font-size:10px; font-weight:700; color:#a78bfa; padding:1px 4px;
              background:#1a1a1a; border-radius:3px; margin-right:4px; }}
.line .text {{ color:#cbd5e1; }}
.legend {{ display:flex; gap:14px; font-size:11px; color:#666; margin: 8px 0 16px; flex-wrap:wrap; }}
.legend .swatch {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }}
</style></head><body>

<div class="wrap">
  <h1>AI commentator v4 — 3-column comparison</h1>
  <p class="sub">Source slice: <code>m05_uni_eval_25min</code>, minutes 5:00–10:00 (Mainz 05 vs Union Berlin, Bundesliga MD33).
   Click any line in any column to seek the video.
   <a href="/experiments/ai_commentator/v3.html">v3 results</a> ·
   <a href="/experiments/ai_commentator/">experiments folder</a>
  </p>

  <div class="note">
   <b>v4 changes from v3:</b>
   (1) scoreline never stated unless score just changed
   (2) team-alias rotation — alternative names rotated in (hosts, visitors, 05ers, die Eisernen, Köpenick, kit colour, manager possessives)
   (3) substitution-board recognition — fourth-official LED parsed as a sub, names looked up by shirt number
   (4) set-piece team attribution — only named when clearly visible
   (5) filler reduction — booth-busy gate widened by 1s
   All five rules are <b>generic</b>, built from info known <b>pre-game</b> only (roster, team metadata).
  </div>

  <div class="video-row">
    <video id="player" controls preload="metadata"
           src="/experiments/ai_commentator/v4_brit_synced.mp4"></video>
    <div class="audio-tabs">
      <button data-src="/experiments/ai_commentator/original_with_human_commentary.mp4">Original broadcast</button>
      <button data-src="/experiments/ai_commentator/v4_brit_synced.mp4" class="active">AI EN — v4 British voice</button>
      <button data-src="/experiments/ai_commentator/v4_fr_synced.mp4">AI FR — v4 Keith voice</button>
      <button data-src="/experiments/ai_commentator/v3_brit_synced.mp4">v3 EN (compare)</button>
    </div>
  </div>

  <div class="legend">
    <span><span class="swatch" style="background:#fbbf24"></span>Soniox gold-corrected STT (real broadcast)</span>
    <span><span class="swatch" style="background:#60a5fa"></span>AI commentator v4 (English)</span>
    <span><span class="swatch" style="background:#f472b6"></span>AI commentator v4 (French)</span>
  </div>

  <div class="cols">
    <div class="col col-soniox">
      <div class="col-head"><span>Soniox gold STT</span><span class="count">{len(gold)} turns</span></div>
      <div class="lines" id="col-gold">{gold_rows(gold)}</div>
    </div>
    <div class="col col-ai-en">
      <div class="col-head"><span>AI English (v4)</span><span class="count">{len(ai_en)} lines</span></div>
      <div class="lines" id="col-ai-en">{ai_rows(ai_en, 'text')}</div>
    </div>
    <div class="col col-ai-fr">
      <div class="col-head"><span>AI French (v4)</span><span class="count">{len(ai_fr)} lines</span></div>
      <div class="lines" id="col-ai-fr">{ai_rows(ai_fr, 'fr')}</div>
    </div>
  </div>
</div>

<script>
  const player = document.getElementById('player');

  // Audio-source toggle: swap src while preserving currentTime.
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

  // Click any line → seek + play
  document.querySelectorAll('.line').forEach(line => {{
    line.addEventListener('click', () => {{
      const t = parseFloat(line.dataset.start);
      player.currentTime = Math.max(0, t - 0.2);
      player.play();
    }});
  }});

  // Highlight currently-playing line(s) in each column
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
print(f"  gold: {len(gold)}  ai_en: {len(ai_en)}  ai_fr: {len(ai_fr)}")
