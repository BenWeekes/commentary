#!/usr/bin/env python3
"""Public results page — two pipelines only. Hides implementation details.

Snapshots each run to /var/www/html/experiments/ai_commentator/<UTC-yyMMdd-HHmm>/
so shared links stay stable when we iterate the pipelines. The parent
/experiments/ai_commentator/results.html always points at the latest run.

Usage:
    python build_public_page.py                # write latest only (no snapshot)
    python build_public_page.py --snapshot     # also copy to /YYMMDD-HHMM/
"""
from __future__ import annotations
import argparse, json, html, shutil
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
PUBLISH_ROOT = Path('/var/www/html/experiments/ai_commentator')

# ---- Pipeline registry --------------------------------------------------
# INTERNAL — the two production pipelines we currently ship. Only labels
# reach the HTML; internal variant names / models / prompts are NOT rendered.

PIPELINES = [
    {
        'label': 'Pipeline 1',
        'tagline': 'Higher accuracy — larger reasoning budget.',
        'fixed_delay_s': 8,
        'internal_variant': 'v20_par_live',
        'en_mp4': 'v20_par_live_en_synced.mp4',
        'fr_mp4': 'v20_par_live_fr_synced.mp4',
        'en_jsonl': 'commentary_v20_par_live_en_tagged.jsonl',
        'fr_jsonl': 'commentary_v20_par_live_fr_tagged.jsonl',
        'notes': 'Live SRT + two-stage: safe-draft vision + text polisher (EN+FR).',
    },
    {
        'label': 'Pipeline 2',
        'tagline': 'Lowest latency — smallest reasoning budget.',
        'fixed_delay_s': 4,
        'internal_variant': 'v18_low_live',
        'en_mp4': 'v18_low_live_en_synced.mp4',
        'fr_mp4': 'v18_low_live_fr_synced.mp4',
        'en_jsonl': 'commentary_v18_low_live_en_tagged.jsonl',
        'fr_jsonl': 'commentary_v18_low_live_fr_tagged.jsonl',
        'notes': 'Live SRT + two-stage: safe-draft vision + text polisher (EN+FR).',
    },
]

ORIGINAL_LABEL = 'Original broadcast'
ORIGINAL_MP4 = 'original_with_human_commentary.mp4'
ORIGINAL_JSONL = 'gold_soniox_5min.jsonl'

# ---- helpers -----------------------------------------------------------

def load_judge(variant):
    p = BASE / f'judge_{variant}.json'
    if not p.exists(): return None
    return json.load(open(p))


def load_leaderboard_row(variant):
    lb_path = BASE / 'leaderboard.json'
    if not lb_path.exists(): return {}
    for v in json.load(open(lb_path))['variants']:
        if v['variant'] == variant:
            return v
    return {}


def fmt_ts(s):
    s = float(s)
    return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"


def transcript_rows(jsonl_path, judge_data=None, lang_key='text', is_original=False):
    p = BASE / jsonl_path
    if not p.exists():
        return '<div class="empty">not available</div>'
    rows = [json.loads(l) for l in open(p) if l.strip()]
    judge_lookup = {}
    if judge_data:
        for v in judge_data.get('verdicts', []):
            judge_lookup[round(float(v.get('_t', 0)), 1)] = v
    out = []
    for r in rows:
        text = r.get(lang_key) or r.get('text') or r.get('fr') or ''
        ts = r.get('natural_start_s') or r.get('scheduled_start_s') or r.get('video_time_s') or r.get('start_s') or 0
        vt_key = round(float(r.get('video_time_s') or r.get('start_s') or 0), 1)
        v = judge_lookup.get(vt_key)
        if not v:
            for k in (vt_key + 0.1, vt_key - 0.1):
                if k in judge_lookup: v = judge_lookup[k]; break

        spk_html = ''
        if is_original:
            spk = r.get('speaker', 0)
            spk_html = f"<span class='spk spk-{spk}'>S{spk}</span>"

        judge_html = ''
        if v:
            hl = v.get('human_likeness', 0)
            hallu = v.get('hallucination_likely', 0)
            rationale = v.get('rationale', '')
            stars = '★'*hl + '☆'*(5-hl)
            cls = 'jbad' if hallu else ('jok' if hl >= 4 else 'jmid')
            badges = [f"<span class='hl'>{stars}</span>"]
            if hallu: badges.append("<span class='flag hallu'>⚠</span>")
            judge_html = (f"<div class='judge {cls}'>"
                          f"{''.join(badges)}"
                          f"<span class='rat'><span class='rat-label'>Judge:</span> {html.escape(rationale)}</span>"
                          f"</div>")

        out.append(
            f"<div class='line' data-start='{ts:.3f}'>"
            f"<span class='ts'>{fmt_ts(ts)}</span>{spk_html}"
            f"<span class='text'>{html.escape(text)}</span>"
            f"{judge_html}"
            f"</div>"
        )
    return ''.join(out)


def measured_lag_p90(variant):
    """Read the measured live pipeline lag (wall - video) p90 in seconds
    from the variant's per-line JSONL, if the file has wall_at_accept_s."""
    for path in (
        BASE / f'commentary_{variant}.jsonl',
        BASE / f'commentary_{variant}_scheduled.jsonl',
    ):
        if not path.exists(): continue
        lags = []
        for line in open(path):
            r = json.loads(line)
            if 'wall_at_accept_s' in r and 'video_time_s' in r:
                lags.append(r['wall_at_accept_s'] - r['video_time_s'])
        if lags:
            lags.sort()
            return round(lags[int(len(lags)*0.9)], 1)
    return None


def pipeline_stats(p):
    """Return summary numbers for a pipeline: lines, judge scores, delay."""
    variant = p['internal_variant']
    lb = load_leaderboard_row(variant)
    lines = lb.get('lines_per_5min', 0)
    hallu = lb.get('judge_hallucination_rate')
    human = lb.get('judge_human_likeness_mean')
    cover = lb.get('soniox_turn_coverage')
    measured_p90 = measured_lag_p90(variant)
    return {
        'lines': lines,
        'accuracy_pct': (int((1-hallu)*100) if hallu is not None else None),
        'hallu_pct': (int(hallu*100) if hallu is not None else None),
        'humanlike': human,
        'coverage_pct': (int(cover*100) if cover is not None else None),
        'measured_lag_p90_s': measured_p90,
    }


# ---- HTML --------------------------------------------------------------

def build_html():
    generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Pipeline summary cards
    cards = []
    for p in PIPELINES:
        s = pipeline_stats(p)
        measured = s.get('measured_lag_p90_s')
        measured_val = f"{measured}" if measured is not None else '—'
        cards.append(f"""
        <div class="pcard">
          <div class="pcard-head">
            <span class="plabel">{html.escape(p['label'])}</span>
            <span class="pdelay">{p['fixed_delay_s']} s fixed delay</span>
          </div>
          <div class="ptag">{html.escape(p['tagline'])}</div>
          <div class="pstats">
            <div class="stat"><div class="k">accuracy</div><div class="v">{s['accuracy_pct'] or '—'}<span class="u">%</span></div></div>
            <div class="stat"><div class="k">human-like</div><div class="v">{f"{s['humanlike']:.2f}" if s['humanlike'] else '—'}<span class="u">/5</span></div></div>
            <div class="stat"><div class="k">coverage</div><div class="v">{s['coverage_pct'] or '—'}<span class="u">%</span></div></div>
            <div class="stat"><div class="k">lines / 5m</div><div class="v">{s['lines']}</div></div>
            <div class="stat"><div class="k">p90 lag</div><div class="v">{measured_val}<span class="u">s</span></div></div>
          </div>
          <div class="pnotes">{html.escape(p['notes'])} Fixed delay chosen to cover the measured p90 pipeline lag with headroom, so commentary always lands in sync with the delayed broadcast video.</div>
        </div>""")

    # Audio-toggle buttons — 5 total (original + 2 pipelines × 2 langs)
    audio_buttons = [f'<button data-src="/experiments/ai_commentator/{ORIGINAL_MP4}">Original</button>']
    default_mp4 = f"/experiments/ai_commentator/{PIPELINES[0]['en_mp4']}"
    for p in PIPELINES:
        active = ' class="active"' if f"/experiments/ai_commentator/{p['en_mp4']}" == default_mp4 else ''
        audio_buttons.append(f'<button data-src="/experiments/ai_commentator/{p["en_mp4"]}"{active}>{html.escape(p["label"])} — EN</button>')
        audio_buttons.append(f'<button data-src="/experiments/ai_commentator/{p["fr_mp4"]}">{html.escape(p["label"])} — FR</button>')

    # Transcript columns — Original + each pipeline EN + FR
    cols = []
    # original
    cols.append(('soniox', ORIGINAL_LABEL, transcript_rows(ORIGINAL_JSONL, is_original=True)))
    for p in PIPELINES:
        j = load_judge(p['internal_variant'])
        cols.append((f"p{p['label'][-1]}-en", f"{p['label']} — EN", transcript_rows(p['en_jsonl'], j)))
        cols.append((f"p{p['label'][-1]}-fr", f"{p['label']} — FR", transcript_rows(p['fr_jsonl'], j, lang_key='text')))

    cols_html = ''.join(
        f"<div class='col col-{cls}'><div class='col-head'>{html.escape(label)}</div>"
        f"<div class='lines'>{rows}</div></div>"
        for cls, label, rows in cols
    )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AI football commentator — pipeline comparison</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#0a0a0a; color:#e0e0e0; }}
.wrap {{ max-width: 1600px; margin: 0 auto; padding: 22px; }}
h1 {{ font-size: 22px; color:#fff; margin-bottom: 4px; }}
p.sub {{ color:#888; font-size:13px; margin-bottom:16px; }}

.section {{ background: #111; border:1px solid #1f1f1f; border-radius:8px;
            padding:14px; margin-bottom:14px; }}
.section h2 {{ font-size:12px; color:#aaa; text-transform:uppercase;
              letter-spacing:0.5px; margin-bottom:10px; }}

.pcards {{ display:grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
.pcard {{ background:#0e0e0e; border:1px solid #262626; border-radius:8px; padding:14px; }}
.pcard-head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }}
.plabel {{ font-size:18px; font-weight:700; color:#fbbf24; }}
.pdelay {{ font-size:12px; color:#4ade80; background:#052e16; padding:3px 8px; border-radius:12px; }}
.ptag {{ color:#aaa; font-size:13px; margin-bottom:12px; }}
.pstats {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:8px; margin-bottom:8px; }}
.judge-explain {{ margin-top: 14px; padding: 8px 12px; background:#0e0e0e;
                  border:1px solid #262626; border-radius:6px; }}
.judge-explain summary {{ cursor:pointer; font-size:12px; color:#a78bfa;
                          font-weight:600; user-select:none; }}
.judge-explain summary:hover {{ color:#c084fc; }}
.jexpl {{ padding: 10px 4px 4px; color:#aaa; font-size:12px; line-height:1.6; }}
.jexpl p {{ margin: 4px 0 8px; }}
.jexpl ol {{ margin: 4px 0 10px 22px; }}
.jexpl table.jrules {{ width:100%; border-collapse:collapse; margin: 8px 0 10px; font-size:11.5px; }}
.jexpl table.jrules th, .jexpl table.jrules td {{ border:1px solid #262626; padding:6px 8px; text-align:left; vertical-align:top; }}
.jexpl table.jrules th {{ background:#181818; color:#e5e7eb; font-weight:700;
                          text-transform:uppercase; font-size:10px; letter-spacing:0.5px; }}
.jexpl table.jrules td:first-child {{ font-family:ui-monospace,monospace; color:#a78bfa; white-space:nowrap; }}
.jexpl code {{ background:#181818; padding:1px 4px; border-radius:3px; font-size:11px; }}
.stat {{ background:#181818; padding:8px 10px; border-radius:6px; }}
.stat .k {{ font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.5px; }}
.stat .v {{ font-size:20px; font-weight:700; color:#e5e7eb; }}
.stat .u {{ font-size:12px; color:#888; margin-left:2px; }}
.pnotes {{ font-size:11px; color:#666; font-style:italic; }}

.video-row video {{ width:100%; max-width:880px; display:block; margin:0 auto 10px; background:#000; border-radius:4px; }}
.audio-tabs {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:center; }}
.audio-tabs button {{ background:#1a1a1a; color:#aaa; border:1px solid #2a2a2a;
                      padding:8px 14px; font-size:12px; border-radius:18px;
                      cursor:pointer; font-weight:600; }}
.audio-tabs button:hover {{ border-color:#3a3a3a; color:#eee; }}
.audio-tabs button.active {{ background:#052e16; color:#4ade80; border-color:#166534; }}

.cols {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:10px; }}
.col {{ background:#0e0e0e; border:1px solid #1f1f1f; border-radius:6px; overflow:hidden; }}
.col-head {{ padding:8px 12px; border-bottom:1px solid #1f1f1f; font-size:12px;
             font-weight:700; text-transform:uppercase; letter-spacing:0.5px; color:#aaa; }}
.col-soniox .col-head {{ color:#fbbf24; }}
.col-p1-en .col-head, .col-p1-fr .col-head {{ color:#fbbf24; }}
.col-p2-en .col-head, .col-p2-fr .col-head {{ color:#60a5fa; }}
.lines {{ max-height:70vh; overflow-y:auto; padding:4px 0; }}
.line {{ padding:6px 10px; cursor:pointer; border-left:3px solid transparent;
         font-size:12px; line-height:1.4; }}
.line:hover {{ background:#1a1a1a; }}
.line.active {{ background:#1a2030; border-left-color:#4ade80; }}
.line .ts {{ display:inline-block; min-width:55px; font-family: ui-monospace, monospace;
             font-size:10px; color:#555; margin-right:6px; }}
.line .spk {{ display:inline-block; font-size:9.5px; font-weight:700;
              padding:1px 4px; border-radius:3px; margin-right:4px;
              background:#1f2937; color:#a78bfa; }}
.line .spk-1 {{ background:#2d2400; color:#fbbf24; }}
.line .judge {{ display:block; margin-top:3px; padding-left:60px; font-size:10.5px; line-height:1.35; }}
.line .judge .hl {{ color:#fbbf24; margin-right:6px; letter-spacing:0.5px; font-size:10px; }}
.line .judge.jbad .hl {{ color:#f87171; }}
.line .judge.jok .hl {{ color:#4ade80; }}
.line .judge .flag {{ font-size:9.5px; font-weight:700; padding:0 4px;
                      border-radius:3px; margin-right:4px; background:#2d0a0a; color:#f87171; }}
.line .judge .rat {{ color:#666; font-style:italic; }}
.line .judge .rat-label {{ color:#a78bfa; font-style:normal; font-weight:700; font-size:9.5px;
                           padding:0 4px; background:#1a1a2e; border-radius:3px; margin-right:3px; }}
.empty {{ padding:30px; text-align:center; color:#555; }}
.foot {{ margin-top:20px; padding-top:16px; border-top:1px solid #262626; color:#555; font-size:11px; }}
</style></head><body>
<div class="wrap">
  <h1>AI football commentator</h1>
  <p class="sub">Two production pipelines running on the same 5-minute slice. Each pipeline generates English + French commentary from the video stream alone (no STT). Judged by an independent LLM against the frame.</p>

  <div class="section">
    <h2>Pipelines</h2>
    <div class="pcards">{''.join(cards)}</div>
    <details class="judge-explain">
      <summary>How the judge scores each line</summary>
      <div class="jexpl">
        <p>Every commentary line is scored by an independent <b>vision LLM</b> (a separate <code>gpt-5.5</code> call, not the model that produced the line). The judge receives <b>two inputs</b>:</p>
        <ol>
          <li>The commentary line text.</li>
          <li>The actual video frame the commentator was speaking about.</li>
        </ol>
        <p>It then answers with strict JSON on three dimensions:</p>
        <table class="jrules">
          <tr><th>Field</th><th>Values</th><th>Meaning</th></tr>
          <tr><td>hallucination_likely</td><td>0 / 1</td><td>1 if the line claims an event not visibly happening in the frame — a save, a sub, a card, a specific action that should be obvious if it were real. Shown as a red ⚠ badge in the transcript.</td></tr>
          <tr><td>subject_present</td><td>0 / 1</td><td>1 if the player or team the line names is plausibly visible in the frame (or the line is about the field state in general).</td></tr>
          <tr><td>human_likeness</td><td>1–5</td><td>5 = sounds like a real broadcaster (natural rhythm, idiomatic, vivid). 3 = passable. 1 = robotic caption. Shown as ★★★☆☆ in the transcript.</td></tr>
          <tr><td>rationale</td><td>string</td><td>One short sentence justifying the score. Displayed after "Judge:" under each line.</td></tr>
        </table>
        <p>Aggregate scores at the top of each pipeline card are computed over all lines in that pipeline's run. <b>Accuracy</b> = 1 − mean(hallucination_likely). <b>Coverage</b> = fraction of Soniox real-broadcaster turns that have an AI line within ±5&nbsp;s.</p>
      </div>
    </details>
  </div>

  <div class="section video-row">
    <h2>Watch — pick an audio track</h2>
    <video id="player" controls preload="metadata" src="{default_mp4}"></video>
    <div class="audio-tabs">{''.join(audio_buttons)}</div>
  </div>

  <div class="section">
    <h2>Transcripts</h2>
    <div class="cols">{cols_html}</div>
  </div>

  <p class="foot">Generated {generated_at}. Click any line to seek the video.</p>
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
        const s = parseFloat(l.dataset.start);
        if (s <= t + 0.05) active = l;
        l.classList.remove('active');
      }}
      if (active) active.classList.add('active');
    }});
  }}
  player.addEventListener('timeupdate', highlight);
</script>
</body></html>
"""


INTERNAL_NOTES_TEMPLATE = """# AI commentator — snapshot notes (INTERNAL, not linked from any public page)

Snapshot slug: **{slug}**
Generated: {generated_at}

## Pipelines shipped in this snapshot

{pipeline_details}

## Reproducing this run

The full pipeline lives in `experiments/ai_commentator/` in the `commentary` repo.

### Pipeline 1 (higher quality, ~10 s delay)
- Vision model: `gpt-5.5`
- Prompt strategy: **playerist** — force `[player] + [generic verb] + [location]`, ban team-name refs, generic-over-incorrect.
- Prompt code: `run_gpt55_variant.py` (VARIANT_PROMPTS["playerist"]) and used verbatim in `live_srt_run.py`.
- Rich context: `rich_context.py` — all 40 roster entries + manager tactical fingerprints + pre-game storylines + referee profile.
- Live SRT loop: `live_srt_run.py`. Uses ffmpeg `-re` to push source over SRT (`srt://127.0.0.1:10082?mode=listener`), a second ffmpeg subscribes as caller and emits 960×540 JPEGs at 0.55 s intervals, then per-burst gpt-5.5 vision + FR translation via gpt-5.4-mini + `eleven_v3` TTS in parallel (EN voice `gU0LNdkMOQCOrPrwtbee`, FR voice `LcKoSBj8CeBInl4bQHtq`).

### Pipeline 2 (fastest, ~3 s delay)
- Vision model: `gpt-5.4-mini`
- Prompt: v5 — sub-event memory, trigram dedup, frame carry-over, dynamic booth-busy gate, alias rotation.
- Prompt code: `run_v5.py`.
- Rich context: enabled via `rich_context.py`.
- Batch runner: `run_v5.py`. In production the same pipeline hooks into an SRT live loop the same way `live_srt_run.py` does (single-vision call, no arbiter).

## Judge

External LLM judge is `gpt-5.5` with vision (`judge.py`). Score dimensions: hallucination_likely (0/1), subject_present (0/1), human_likeness (1-5), Soniox-turn coverage. Sampled at up to 200 per variant (full coverage for both pipelines here).

## Files in this snapshot

Every MP4 / JSONL / judge referenced by the public results.html has been copied here so this folder is self-contained. Deleting other snapshot folders or the parent index does not break this one.
"""


def snapshot(doc, snapshot_slug, generated_at):
    """Copy the page + all referenced MP4/JSONL into a timestamped subdir."""
    dst = PUBLISH_ROOT / snapshot_slug
    dst.mkdir(parents=True, exist_ok=True)

    # Rewrite absolute paths (/experiments/ai_commentator/...) to relative ones
    # so the snapshot is self-contained.
    snap_doc = doc.replace('/experiments/ai_commentator/', './')
    (dst / 'results.html').write_text(snap_doc)

    referenced = {ORIGINAL_MP4, ORIGINAL_JSONL}
    for p in PIPELINES:
        referenced.update([p['en_mp4'], p['fr_mp4'], p['en_jsonl'], p['fr_jsonl']])
        referenced.add(f"judge_{p['internal_variant']}.json")

    copied = []; missing = []
    for name in sorted(referenced):
        src = None
        for c in (BASE / name, PUBLISH_ROOT / name):
            if c.exists():
                src = c; break
        if src is None:
            missing.append(name); continue
        shutil.copy2(src, dst / name)
        copied.append(name)

    # Internal notes.md — describes what pipelines are, how to reproduce.
    # Not linked from any public page. Useful when we come back to this snapshot.
    pipeline_details = []
    for p in PIPELINES:
        s = pipeline_stats(p)
        pipeline_details.append(
            f"- **{p['label']}** (internal variant `{p['internal_variant']}`)\n"
            f"    - fixed broadcast delay: **{p['fixed_delay_s']} s**  (measured p90 pipeline lag: {s.get('measured_lag_p90_s')} s)\n"
            f"    - lines / 5 min: {s['lines']}   coverage: {s['coverage_pct']}%\n"
            f"    - judge accuracy (1 − hallu): {s['accuracy_pct']}%   human-likeness: {s['humanlike']}/5\n"
            f"    - MP4s: `{p['en_mp4']}`, `{p['fr_mp4']}`\n"
            f"    - transcript JSONLs: `{p['en_jsonl']}`, `{p['fr_jsonl']}`"
        )
    (dst / 'notes.md').write_text(INTERNAL_NOTES_TEMPLATE.format(
        slug=snapshot_slug,
        generated_at=generated_at,
        pipeline_details='\n'.join(pipeline_details),
    ))

    return dst, copied, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snapshot', action='store_true', help='Also write to timestamped subdir')
    args = ap.parse_args()

    generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    doc = build_html()

    latest = BASE / 'results.html'
    latest.write_text(doc)
    print(f"Wrote {latest} ({len(doc)/1024:.0f} KB)")

    # Publish latest (sudo cp handles root-owned files from earlier runs)
    import subprocess
    subprocess.run(['sudo', 'cp', str(latest), str(PUBLISH_ROOT / 'results.html')], check=True)
    print(f"Published to {PUBLISH_ROOT/'results.html'}")

    if args.snapshot:
        slug = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
        dst, copied, missing = snapshot(doc, slug, generated_at)
        print(f"Snapshot: /experiments/ai_commentator/{slug}/results.html")
        print(f"  {len(copied)} files copied, {len(missing)} missing: {missing[:3]}")


if __name__ == '__main__':
    main()
