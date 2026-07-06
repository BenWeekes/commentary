#!/usr/bin/env python3
"""v6 results page: leaderboard + transcript columns + audio toggle."""
import json, html
from pathlib import Path

BASE = Path('/home/ubuntu/commentary/experiments/ai_commentator')
OUT = BASE / 'results.html'
LB = BASE / 'leaderboard.json'

board = json.load(open(LB))
variants = board['variants']

# Load per-line judge verdicts so we can show humanlike score + hallucination flag + rationale on each line
def load_judge(variant):
    p = BASE / f'judge_{variant}.json'
    if not p.exists(): return {}
    data = json.load(open(p))
    out = {}
    for v in data.get('verdicts', []):
        # round t to one decimal so we can match by timestamp
        key = round(float(v.get('_t', 0)), 1)
        out[key] = v
    return out

# Order: Soniox first, then by lines per 5min ascending (closer to real is better at top)
gold_row = next((v for v in variants if v['variant'] == 'soniox_gold'), None)
others = [v for v in variants if v['variant'] != 'soniox_gold']
others.sort(key=lambda v: abs(v.get('lines_per_5min', 0) - (gold_row['lines_per_5min'] if gold_row else 35)))
ordered = ([gold_row] if gold_row else []) + others

# Variant → published media + transcript jsonl
MEDIA = {
    'soniox_gold': {'mp4': '/experiments/ai_commentator/original_with_human_commentary.mp4', 'jsonl': 'gold_soniox_5min.jsonl', 'speaker_col': True},
    'v4_en': {'mp4': '/experiments/ai_commentator/v4_brit_synced.mp4', 'jsonl': 'commentary_v4_en_tagged.jsonl'},
    'v5_en': {'mp4': '/experiments/ai_commentator/v5_brit_synced.mp4', 'jsonl': 'commentary_v5_en_tagged.jsonl'},
    'v5_fr': {'mp4': '/experiments/ai_commentator/v5_fr_synced.mp4', 'jsonl': 'commentary_v5_fr_tagged.jsonl', 'lang_key': 'fr'},
    'gemini_en': {'mp4': '/experiments/ai_commentator/gemini_brit_synced.mp4', 'jsonl': 'commentary_gemini_en_tagged.jsonl'},
    'gpt55_en': {'mp4': '/experiments/ai_commentator/gpt55_brit_synced.mp4', 'jsonl': 'commentary_gpt55_scheduled.jsonl'},
    'gpt55_quiet': {'mp4': '/experiments/ai_commentator/gpt55_quiet_brit_synced.mp4', 'jsonl': 'commentary_gpt55_quiet_tagged.jsonl'},
    'gpt55_long': {'mp4': '/experiments/ai_commentator/gpt55_long_brit_synced.mp4', 'jsonl': 'commentary_gpt55_long_tagged.jsonl'},
    'gpt55_playerist': {'mp4': '/experiments/ai_commentator/gpt55_playerist_brit_synced.mp4', 'jsonl': 'commentary_gpt55_playerist_tagged.jsonl'},
    'gpt55_en_tagged': {'mp4': '/experiments/ai_commentator/gpt55_brit_synced.mp4', 'jsonl': 'commentary_gpt55_en_tagged.jsonl'},
    'v7_hybrid': {'mp4': '/experiments/ai_commentator/v7_hybrid_brit_synced.mp4', 'jsonl': 'commentary_v7_en_tagged.jsonl'},
    'v8_hybrid': {'mp4': '/experiments/ai_commentator/v8_brit_synced.mp4', 'jsonl': 'commentary_v8_en_tagged.jsonl'},
    'v8_fr': {'mp4': '/experiments/ai_commentator/v8_fr_synced.mp4', 'jsonl': 'commentary_v8_fr_tagged.jsonl', 'lang_key': 'text'},
    'v8a': {'mp4': '/experiments/ai_commentator/v8a_brit_synced.mp4', 'jsonl': 'commentary_v8a_en_tagged.jsonl'},
    'v8a_fr': {'mp4': '/experiments/ai_commentator/v8a_fr_synced.mp4', 'jsonl': 'commentary_v8a_fr_tagged.jsonl', 'lang_key': 'text'},
    'v8b': {'mp4': None, 'jsonl': 'commentary_v8b_scheduled.jsonl'},
    'v8c': {'mp4': None, 'jsonl': 'commentary_v8c_scheduled.jsonl'},
    'v8d': {'mp4': '/experiments/ai_commentator/v8d_brit_synced.mp4', 'jsonl': 'commentary_v8d_en_tagged.jsonl'},
    'v8d_fr': {'mp4': '/experiments/ai_commentator/v8d_fr_synced.mp4', 'jsonl': 'commentary_v8d_fr_tagged.jsonl', 'lang_key': 'text'},
    'gpt54_en': {'mp4': None, 'jsonl': 'commentary_gpt54_scheduled.jsonl'},
    'gpt55_playerist_en': {'mp4': '/experiments/ai_commentator/gpt55_playerist_brit_synced.mp4', 'jsonl': 'commentary_gpt55_playerist_tagged.jsonl'},
    'gpt55_playerist_fr': {'mp4': '/experiments/ai_commentator/gpt55_playerist_fr_synced.mp4', 'jsonl': 'commentary_gpt55_playerist_fr_tagged.jsonl', 'lang_key': 'text'},
    'v12': {'mp4': None, 'jsonl': 'commentary_v12_scheduled.jsonl'},
    'v13_live_en': {'mp4': '/experiments/ai_commentator/v13_live_en_synced.mp4', 'jsonl': 'commentary_v13_live_en_tagged.jsonl'},
    'v13_live_fr': {'mp4': '/experiments/ai_commentator/v13_live_fr_synced.mp4', 'jsonl': 'commentary_v13_live_fr_tagged.jsonl', 'lang_key': 'text'},
}


def fmt_ts(s):
    s = float(s)
    return f"{int(s//60):02d}:{int(s%60):02d}.{int(s*1000)%1000:03d}"


def fmt_metric(v, default='—'):
    if v is None: return default
    if isinstance(v, float): return f"{v:.2f}"
    return str(v)


def rows_for_variant(name):
    media = MEDIA.get(name)
    if not media: return ''
    p = BASE / media['jsonl']
    if not p.exists(): return f"<div class='empty'>not yet rendered</div>"
    rows = [json.loads(l) for l in open(p) if l.strip()]
    out = []
    lang_key = media.get('lang_key', 'text')
    # Build per-line judge lookup. Match against video_time_s (raw clock), not natural_start_s.
    judge_lookup = load_judge(name)
    for r in rows:
        text = r.get(lang_key) or r.get('text') or r.get('fr') or ''
        ts = r.get('natural_start_s') or r.get('scheduled_start_s') or r.get('video_time_s') or r.get('start_s') or 0
        # judge_lookup keys by video_time_s rounded to 1dp
        vt_key = round(float(r.get('video_time_s') or r.get('start_s') or 0), 1)
        verdict = judge_lookup.get(vt_key)
        # also try ±0.05 since rounding can disagree
        if not verdict:
            for k in (vt_key + 0.1, vt_key - 0.1):
                if k in judge_lookup:
                    verdict = judge_lookup[k]; break
        spk_html = ''
        if media.get('speaker_col'):
            spk = r.get('speaker', 0)
            spk_html = f"<span class='spk spk-{spk}'>S{spk}</span>"
        tag = r.get('tag', '')
        tag_html = f"<span class='tag'>{html.escape(tag)}</span> " if tag else ''
        sub_html = ''
        if r.get('sub_detected'):
            off, on = r['sub_detected']
            sub_html = f"<span class='sub'>↻ {html.escape(off)}↔{html.escape(on)}</span> "
        # judge badge
        judge_html = ''
        if verdict:
            hl = verdict.get('human_likeness', 0)
            hallu = verdict.get('hallucination_likely', 0)
            subj = verdict.get('subject_present', 1)
            rationale = verdict.get('rationale', '')
            stars = '★' * hl + '☆' * (5 - hl)
            cls = 'jbad' if hallu else ('jok' if hl >= 4 else 'jmid')
            badges = []
            badges.append(f"<span class='hl'>{stars}</span>")
            if hallu: badges.append("<span class='flag hallu' title='judge says: possible hallucination'>⚠ hallu</span>")
            if not subj: badges.append("<span class='flag subj' title='subject not in frame'>✗ subj</span>")
            judge_html = (
                f"<div class='judge {cls}'>"
                f"{''.join(badges)}"
                f"<span class='rat'>{html.escape(rationale)}</span>"
                f"</div>"
            )
        out.append(
            f"<div class='line' data-start='{ts:.3f}'>"
            f"<span class='ts'>{fmt_ts(ts)}</span>{spk_html}"
            f"<span class='text'>{tag_html}{sub_html}{html.escape(text)}</span>"
            f"{judge_html}"
            f"</div>"
        )
    return ''.join(out)


# Pipeline latency lookup — vision p90 + TTS first-byte (0.3s) + natural reaction (0.3s)
# Sourced from each variant's per-line JSONL.
def pipeline_p90(variant):
    # Try various source JSONL paths
    candidates = [
        BASE / f'commentary_{variant}.jsonl',
        BASE / f'commentary_{variant}_scheduled.jsonl',
        BASE / f'commentary_{variant.replace("_en", "")}.jsonl',
    ]
    for p in candidates:
        if not p.exists(): continue
        lats = []
        for line in open(p):
            r = json.loads(line)
            lat = r.get('vision_latency_ms') or r.get('total_ms')
            if lat: lats.append(lat)
        if lats:
            lats.sort()
            p90 = lats[int(len(lats)*0.9)]
            return round((p90 + 300 + 300) / 1000, 1)  # add TTS + natural
    return None


# Enrich leaderboard with pipeline latency
for v in variants:
    if v['variant'] == 'soniox_gold':
        v['pipeline_p90_s'] = None
        continue
    # Map variant name to source jsonl base
    src = v['variant'].replace('_en', '') if v['variant'].endswith('_en') else v['variant']
    p90 = pipeline_p90(src) or pipeline_p90(v['variant'])
    v['pipeline_p90_s'] = p90


# Build leaderboard table
metrics_order = [
    ('lines_per_5min', 'lines/5m'),
    ('mean_gap_s', 'gap μ'),
    ('pipeline_p90_s', 'pipe p90 s'),
    ('trigram_repeat_rate', 'trigm rep'),
    ('type_token_ratio', 'TTR'),
    ('alias_entropy_bits', 'alias H'),
    ('player_name_density', 'player ρ'),
    ('action_verb_density', 'verb ρ'),
    ('avg_words_per_line', 'words/ln'),
    ('mainz_mentions', '"Mainz"'),
    ('union_mentions', '"Union"'),
    ('judge_hallucination_rate', 'hallu ↓'),
    ('judge_human_likeness_mean', 'humanlike ↑'),
    ('judge_subject_present_rate', 'subj OK'),
    ('soniox_turn_coverage', 'coverage'),
]


def lb_header():
    cells = "".join(f"<th title='{key}'>{label}</th>" for key, label in metrics_order)
    return f"<tr><th class='variant-col'>variant</th>{cells}<th>notes</th></tr>"


def lb_row(v, is_gold=False):
    cls = 'gold' if is_gold else ''
    cells = []
    for key, _ in metrics_order:
        val = v.get(key)
        cells.append(f"<td>{fmt_metric(val)}</td>")
    notes = html.escape(v.get('notes', ''))
    return f"<tr class='{cls}'><td class='variant-col'><b>{html.escape(v['variant'])}</b></td>{''.join(cells)}<td class='notes'>{notes}</td></tr>"


lb_html = "<table class='lb'>" + lb_header() + "".join(lb_row(v, v['variant']=='soniox_gold') for v in ordered) + "</table>"

# Transcript columns - LIVE run (top row) + two latency tiers side by side
cols_to_show = ['soniox_gold', 'v13_live_en', 'v13_live_fr', 'v5_en', 'v5_fr']

variant_lookup = {v['variant']: v for v in variants}
cols_html_parts = []
for cname in cols_to_show:
    if cname not in MEDIA: continue
    label = {
        'soniox_gold': 'Soniox (real)',
        'gpt55_en': 'gpt-5.5',
        'gpt55_long': 'gpt-5.5 long ★ best human',
        'gpt55_playerist': 'gpt-5.5 playerist ★ best all-rounder',
        'gpt55_quiet': 'gpt-5.5 quiet',
        'gemini_en': 'Gemini',
        'gpt54_en': 'gpt-5.4 (full)',
        'v5_en': 'LOW-LAT ★ gpt-5.4-mini v5 EN (3s)',
        'v5_fr': 'LOW-LAT gpt-5.4-mini v5 FR (3s)',
        'v7_hybrid': 'v7 hybrid (mini + Gemini)',
        'v8_hybrid': 'v8 base',
        'v8_fr': 'v8 base FR',
        'v8a': 'v8a ★ strict rubric',
        'v8a_fr': 'v8a French',
        'v8b': 'v8b (gpt-5.5 arb, slow)',
        'v8c': 'v8c (strict+5.5, slow)',
        'v8d': 'v8d ★ conf-gated vision',
        'v8d_fr': 'v8d French',
        'gpt55_playerist_en': 'HIGH-LAT ★ gpt-5.5 playerist EN (9s)',
        'gpt55_playerist_fr': 'HIGH-LAT gpt-5.5 playerist FR (9s)',
        'v12': 'v12 Gemini + playerist',
        'v13_live_en': 'LIVE SRT ★ gpt-5.5 playerist EN',
        'v13_live_fr': 'LIVE SRT gpt-5.5 playerist FR',
    }[cname]
    p = BASE / MEDIA[cname]['jsonl']
    count = sum(1 for _ in open(p)) if p.exists() else 0
    rows = rows_for_variant(cname)
    # judge summary box for this variant
    lb_v = variant_lookup.get(cname, {})
    judge_box = ''
    if 'judge_human_likeness_mean' in lb_v:
        hl = lb_v['judge_human_likeness_mean']
        hallu = lb_v.get('judge_hallucination_rate', 0)
        cov = lb_v.get('soniox_turn_coverage', 0)
        subj = lb_v.get('judge_subject_present_rate', 0)
        n = lb_v.get('judge_sample_n', 0)
        judge_box = (
            f"<div class='judge-summary'>"
            f"<span class='js-item'>humanlike <b>{hl:.2f}</b>/5</span>"
            f"<span class='js-item js-{'bad' if hallu > 0.2 else 'ok'}'>hallu <b>{hallu*100:.0f}%</b></span>"
            f"<span class='js-item'>subj <b>{subj*100:.0f}%</b></span>"
            f"<span class='js-item'>cover <b>{cov*100:.0f}%</b></span>"
            f"<span class='js-meta'>(judge n={n})</span>"
            f"</div>"
        )
    cols_html_parts.append(
        f"<div class='col col-{cname}'>"
        f"<div class='col-head'><span>{html.escape(label)}</span><span class='count'>{count} lines</span></div>"
        f"{judge_box}"
        f"<div class='lines'>{rows}</div></div>"
    )
cols_html = "<div class='cols'>" + "".join(cols_html_parts) + "</div>"

# Audio buttons (skip rows whose MP4 doesn't exist)
def published(name):
    if name == 'soniox_gold': return True
    media = MEDIA.get(name)
    if not media: return False
    return Path(f"/var/www/html{media['mp4']}").exists()


audio_buttons = []
button_specs = [
    ('soniox_gold', 'Original broadcast'),
    ('v13_live_en', 'LIVE SRT ★ EN'),
    ('v13_live_fr', 'LIVE SRT ★ FR'),
    ('gpt55_playerist_en', 'HIGH-LAT gpt-5.5 playerist EN (9s)'),
    ('gpt55_playerist_fr', 'HIGH-LAT gpt-5.5 playerist FR (9s)'),
    ('v5_en', 'LOW-LAT v5 EN (3s)'),
    ('v5_fr', 'LOW-LAT v5 FR (3s)'),
    ('v8a', 'v8a strict rubric'),
    ('v8a_fr', 'v8a French'),
    ('v8d', 'v8d conf-gated'),
    ('v8d_fr', 'v8d French'),
    ('v8_hybrid', 'v8 base'),
    ('v7_hybrid', 'v7 hybrid'),
    ('gpt55_en', 'gpt-5.5 baseline'),
    ('gpt55_quiet', 'gpt-5.5 quiet'),
    ('gpt55_long', 'gpt-5.5 long sentences'),
    ('gpt55_playerist', 'gpt-5.5 player-first'),
    ('gemini_en', 'Gemini 2.5 flash'),
    ('v5_en', 'gpt-5.4-mini v5'),
    ('v5_fr', 'v5 French'),
    ('v4_en', 'v4 (compare)'),
]
default_mp4 = MEDIA['v13_live_en']['mp4'] if published('v13_live_en') else MEDIA['v5_en']['mp4']
for name, label in button_specs:
    if not published(name): continue
    mp4 = MEDIA[name]['mp4']
    active = ' class="active"' if mp4 == default_mp4 else ''
    audio_buttons.append(f"<button data-src='{mp4}'{active}>{html.escape(label)}</button>")
audio_html = "".join(audio_buttons)


doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AI commentator — live SRT + leaderboard</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#0a0a0a; color:#e0e0e0; min-height:100vh; }}
.wrap {{ max-width: 1800px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 22px; color:#fff; margin-bottom: 6px; }}
p.sub {{ color:#888; font-size: 13px; margin-bottom: 14px; }}
p.sub a {{ color:#60a5fa; text-decoration: none; }}
.section {{ background: #111; border: 1px solid #1f1f1f; border-radius: 8px; padding: 14px; margin-bottom: 14px; }}
.section h2 {{ font-size: 13px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }}

/* leaderboard */
table.lb {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
table.lb th, table.lb td {{ padding: 6px 8px; text-align: right; border-bottom: 1px solid #1a1a1a; }}
table.lb th {{ background: #181818; color: #888; font-weight: 600; font-size: 10.5px; text-transform: uppercase; }}
table.lb td:first-child, table.lb th:first-child {{ text-align: left; }}
table.lb td.variant-col {{ font-weight: 600; color: #cbd5e1; }}
table.lb td.notes {{ text-align: left; font-size: 11px; color: #666; max-width: 240px; }}
table.lb tr.gold td {{ background: #2d2400; color: #fbbf24; }}
table.lb tr.gold td.variant-col {{ color: #fbbf24; }}

/* video */
.video-row video {{ width: 100%; max-width: 880px; display: block; margin: 0 auto 10px; background: #000; border-radius: 4px; }}
.audio-tabs {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }}
.audio-tabs button {{ background: #1a1a1a; color: #aaa; border: 1px solid #2a2a2a; padding: 7px 13px;
                      font-size: 12px; border-radius: 18px; cursor: pointer; font-weight: 600; }}
.audio-tabs button:hover {{ border-color: #3a3a3a; color: #eee; }}
.audio-tabs button.active {{ background: #052e16; color: #4ade80; border-color: #166534; }}

/* columns */
.cols {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
.col {{ background: #0e0e0e; border: 1px solid #1f1f1f; border-radius: 6px; overflow: hidden; }}
.col-head {{ padding: 8px 12px; border-bottom: 1px solid #1f1f1f; font-size: 11.5px;
             font-weight: 700; text-transform: uppercase; color: #aaa; display:flex; justify-content: space-between; }}
.col-head .count {{ color: #555; font-weight: 500; }}
.col-soniox_gold .col-head {{ color: #fbbf24; }}
.col-gpt55_en .col-head {{ color: #34d399; }}
.col-gpt55_long .col-head {{ color: #34d399; }}
.col-gpt55_playerist .col-head {{ color: #34d399; }}
.col-gpt55_quiet .col-head {{ color: #34d399; }}
.col-gemini_en .col-head {{ color: #60a5fa; }}
.col-v5_en .col-head {{ color: #f472b6; }}
.col-v7_hybrid .col-head {{ color: #c084fc; }}
.col-v8_hybrid .col-head {{ color: #ff7ab0; }}
.col-v8_fr .col-head {{ color: #ff7ab0; }}
.col-v8a .col-head {{ color: #ff7ab0; }}
.col-v8d .col-head {{ color: #ff9e40; }}
.col-gpt55_playerist_en .col-head {{ color: #4ade80; }}
.col-gpt55_playerist_fr .col-head {{ color: #4ade80; }}
.col-v5_en .col-head {{ color: #60a5fa; }}
.col-v5_fr .col-head {{ color: #60a5fa; }}
.col-v13_live_en .col-head {{ color: #fbbf24; }}
.col-v13_live_fr .col-head {{ color: #fbbf24; }}
.cols {{ grid-template-columns: repeat(5, 1fr) !important; }}
.lines {{ max-height: 70vh; overflow-y: auto; padding: 4px 0; }}
.line {{ padding: 6px 10px; cursor: pointer; border-left: 3px solid transparent;
         font-size: 12px; line-height: 1.4; }}
.line:hover {{ background: #1a1a1a; }}
.line.active {{ background: #1a2030; border-left-color: #4ade80; }}
.line .ts {{ display: inline-block; min-width: 55px; font-family: ui-monospace, monospace;
             font-size: 10px; color: #555; margin-right: 6px; }}
.line .tag {{ font-size: 9.5px; font-weight: 700; color: #a78bfa; padding: 1px 4px;
              background: #1a1a1a; border-radius: 3px; margin-right: 4px; }}
.line .sub {{ font-size: 9.5px; font-weight: 700; color: #fbbf24; padding: 1px 4px;
              background: #2d2400; border-radius: 3px; margin-right: 4px; }}
.line .spk {{ display: inline-block; font-size: 9.5px; font-weight: 700;
              padding: 1px 4px; border-radius: 3px; margin-right: 4px; background: #1f2937; color: #a78bfa; }}
.line .spk-1 {{ background: #2d2400; color: #fbbf24; }}
.line .judge {{ display: block; margin-top: 4px; padding-left: 65px; font-size: 10.5px; line-height: 1.35; }}
.line .judge .hl {{ display: inline-block; color: #fbbf24; margin-right: 6px; letter-spacing: 0.5px; font-size: 10px; }}
.line .judge.jbad .hl {{ color: #f87171; }}
.line .judge.jok .hl {{ color: #4ade80; }}
.line .judge .flag {{ display: inline-block; font-size: 9.5px; font-weight: 700;
                      padding: 0 4px; border-radius: 3px; margin-right: 4px; }}
.line .judge .flag.hallu {{ background: #2d0a0a; color: #f87171; }}
.line .judge .flag.subj {{ background: #2d1f00; color: #fbbf24; }}
.line .judge .rat {{ color: #666; font-style: italic; }}
.line .judge.jbad {{ background: rgba(248,113,113,0.05); }}
.line .judge.jok {{ background: rgba(74,222,128,0.04); }}
.judge-summary {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 6px 12px;
                  border-bottom: 1px solid #1f1f1f; background: #0a0a0a; font-size: 10.5px;
                  color: #888; }}
.judge-summary .js-item {{ padding: 2px 6px; border-radius: 3px; background: #181818; }}
.judge-summary .js-item b {{ color: #ddd; font-weight: 700; }}
.judge-summary .js-item.js-bad b {{ color: #f87171; }}
.judge-summary .js-item.js-ok b {{ color: #4ade80; }}
.judge-summary .js-meta {{ color: #444; margin-left: auto; align-self: center; }}
.empty {{ padding: 30px; text-align: center; color: #555; }}
.legend-text {{ font-size: 11px; color: #666; margin-top: 6px; line-height: 1.5; }}
</style></head><body>

<div class="wrap">
  <h1>AI commentator — real-live SRT + full leaderboard</h1>
  <p class="sub">Same 5-min slice (m05_uni_eval_25min minutes 5:00–10:00). Click any transcript line to seek the video. <a href="/experiments/ai_commentator/v5.html">v5 page</a> · <a href="/experiments/ai_commentator/">all experiments</a>.</p>

  <div class="section" style="border-left:3px solid #fbbf24;">
    <h2>★ Live SRT run — v13_live (gpt-5.5 playerist, real send/receive)</h2>
    <p class="legend-text">
      <b>Wall time:</b> 310 s total for a 300 s clip (10 s tail-flush). <b>Live pipeline lag (wall &minus; video):</b> p50 <b>4.6 s</b>, p90 <b>7.8 s</b>. <b>Lines:</b> 50 accepted. <b>Real sub detected:</b> Trimmel → Juranovic at 264 s (real).
      Pipeline: <code>ffmpeg -re</code> pushes source to <code>srt://127.0.0.1:10082</code>, this run's frame reader pulls at 0.55&nbsp;s intervals via a second ffmpeg subscribed to the SRT stream, each 4-frame burst runs through <code>gpt-5.5 + playerist</code>, results translate to French, then <code>eleven_v3</code> TTS both languages in parallel. All in wall time.
    </p>
  </div>

  <div class="section">
    <h2>Leaderboard</h2>
    {lb_html}
    <p class="legend-text">↑ = higher is better, ↓ = lower is better. <b>Reference</b>: soniox_gold (real broadcaster) — match it on cadence/TTR/coverage, beat it on alias rotation. Detailed metric definitions in <code>experiments/ai_commentator/score.py</code>.</p>
  </div>

  <div class="section video-row">
    <h2>Video — pick an audio track</h2>
    <video id="player" controls preload="metadata" src="{default_mp4}"></video>
    <div class="audio-tabs">{audio_html}</div>
  </div>

  <div class="section">
    <h2>Transcripts (4 selected variants)</h2>
    {cols_html}
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
print(f"Wrote {OUT} ({len(doc)/1024:.0f} KB) with {len(ordered)} variants in leaderboard, {len(cols_to_show)} transcript columns")
