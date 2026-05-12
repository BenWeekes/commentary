#!/usr/bin/env python3
"""Build a static STT gold-vs-realtime comparison page."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def words(s: str) -> list[str]:
    return norm_text(s).split()


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if x == y else 1),
            ))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    aw = words(a)
    bw = words(b)
    if not aw and not bw:
        return 1.0
    if not aw or not bw:
        return 0.0
    # Token overlap is easier to reason about in a review page than sequence ratio.
    aset = set(aw)
    bset = set(bw)
    return len(aset & bset) / max(1, len(aset | bset))


def parse_iso_z(s: str | None) -> float | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def hms_ms(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


def utc_hms_ms(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    d = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def speaker_label(v) -> str:
    if v is None or v == -1:
        return "-"
    try:
        return f"S{int(float(v))}"
    except (TypeError, ValueError):
        return f"S{v}"


def build_rows(gold: list[dict], hyp: list[dict], source_epoch: float | None) -> tuple[list[dict], dict]:
    rows = []
    total_edits = 0
    total_words = 0
    for g in gold:
        start = float(g.get("start", 0.0))
        end = float(g.get("end", start))
        overlapping = [
            t for t in hyp
            if float(t.get("end", 0.0)) >= start - 0.5
            and float(t.get("start", 0.0)) <= end + 0.5
        ]
        overlapping.sort(key=lambda t: (float(t.get("start", 0.0)), float(t.get("end", 0.0))))
        hyp_text = " ".join(str(t.get("text", "")).strip() for t in overlapping).strip()
        gw = words(str(g.get("text", "")))
        hw = words(hyp_text)
        edits = edit_distance(gw, hw)
        wer = edits / max(1, len(gw))
        total_edits += edits
        total_words += max(1, len(gw))
        rows.append({
            "start": start,
            "end": end,
            "time_utc": utc_hms_ms((source_epoch + start) if source_epoch else parse_iso_z(g.get("source_utc_iso"))),
            "offset": f"{hms_ms(start)}-{hms_ms(end)}",
            "speaker": speaker_label(g.get("speaker")),
            "gold": str(g.get("text", "")),
            "hyp": hyp_text,
            "hyp_turns": overlapping,
            "hyp_turn_count": len(overlapping),
            "wer": wer,
            "similarity": similarity(str(g.get("text", "")), hyp_text),
        })
    summary = {
        "rows": len(rows),
        "wer": total_edits / max(1, total_words),
        "total_edits": total_edits,
        "total_words": total_words,
    }
    return rows, summary


def css_class(row: dict) -> str:
    if row["wer"] <= 0.15:
        return "good"
    if row["wer"] <= 0.45:
        return "warn"
    return "bad"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="match_data/m05_uni_md33/eval/20260510_190915/gold_soniox_corrected/turns.json")
    ap.add_argument("--hyp", default="match_data/m05_uni_md33/eval/20260510_190915/live_stt_tuning_full_20260512/soniox_rt_endpoint1000/turns.json")
    ap.add_argument("--label", default="Soniox realtime endpoint=1000ms")
    ap.add_argument("--out", default="stt_compare_m05_uni_md33_soniox1000.html")
    args = ap.parse_args()

    gold = json.load(open(args.gold))
    hyp = json.load(open(args.hyp))
    source_epoch = parse_iso_z(gold[0].get("source_utc_iso")) - float(gold[0].get("start", 0.0)) if gold else None
    rows, summary = build_rows(gold, hyp, source_epoch)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    html_rows = []
    for row in rows:
        hyp_turn_bits = []
        for t in row["hyp_turns"]:
            hyp_turn_bits.append(
                f"<div class=\"turn\"><span>{html.escape(hms_ms(float(t.get('start', 0))))}"
                f"-{html.escape(hms_ms(float(t.get('end', 0))))}</span> "
                f"{html.escape(str(t.get('text', '')))}</div>"
            )
        html_rows.append(f"""
<tr class="{css_class(row)}">
  <td>{html.escape(row['time_utc'])}</td>
  <td>{html.escape(row['offset'])}</td>
  <td>{html.escape(row['speaker'])}</td>
  <td>{row['wer']:.2f}</td>
  <td>{row['similarity']:.2f}</td>
  <td>{row['hyp_turn_count']}</td>
  <td>{html.escape(row['gold'])}</td>
  <td>{html.escape(row['hyp'])}<details><summary>turns</summary>{''.join(hyp_turn_bits)}</details></td>
</tr>""")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>M05 vs UNI STT Gold vs Realtime</title>
<style>
:root {{ color-scheme: light; --border:#d7dee8; --head:#0f172a; --muted:#64748b; --bg:#f8fafc; --good:#ecfdf5; --warn:#fffbeb; --bad:#fef2f2; }}
body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; color:#111827; background:white; }}
header {{ padding:22px 28px 16px; border-bottom:1px solid var(--border); background:var(--bg); position:sticky; top:0; z-index:3; }}
h1 {{ margin:0 0 8px; font-size:24px; color:var(--head); }}
.meta {{ display:flex; flex-wrap:wrap; gap:10px 18px; color:var(--muted); font-size:14px; }}
main {{ padding:22px 28px 40px; }}
.controls {{ margin:0 0 14px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
input, select {{ font:inherit; padding:8px 10px; border:1px solid var(--border); border-radius:6px; }}
input {{ min-width:320px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ border:1px solid var(--border); padding:8px 9px; vertical-align:top; }}
th {{ background:#eef2f7; text-align:left; position:sticky; top:86px; z-index:2; }}
td:nth-child(1), td:nth-child(2), td:nth-child(3), td:nth-child(4), td:nth-child(5), td:nth-child(6) {{ white-space:nowrap; color:#334155; }}
tr.good {{ background:var(--good); }}
tr.warn {{ background:var(--warn); }}
tr.bad {{ background:var(--bad); }}
details {{ margin-top:6px; color:#475569; }}
summary {{ cursor:pointer; }}
.turn {{ margin:4px 0; }}
.turn span {{ color:#64748b; font-variant-numeric: tabular-nums; }}
.hidden {{ display:none; }}
</style></head><body>
<header><h1>M05 vs UNI STT Gold vs Realtime</h1>
<div class="meta">
<span>Gold: {html.escape(Path(args.gold).as_posix())}</span>
<span>Realtime: {html.escape(args.label)}</span>
<span>Rows: {summary['rows']}</span>
<span>Window WER: {summary['wer']:.3f}</span>
<span>Generated: {generated}</span>
</div></header>
<main>
<div class="controls">
<input id="filter" placeholder="Filter gold or realtime text">
<select id="level"><option value="all">All rows</option><option value="bad">WER &gt; 0.45</option><option value="warn">WER 0.15-0.45</option><option value="good">WER <= 0.15</option><option value="multi">Multiple realtime turns</option></select>
</div>
<table id="rows"><thead><tr><th>Time UTC</th><th>Source Offset</th><th>Speaker</th><th>WER</th><th>Overlap</th><th>Turns</th><th>Gold</th><th>Realtime</th></tr></thead>
<tbody>{''.join(html_rows)}</tbody></table>
</main>
<script>
const filter = document.getElementById('filter');
const level = document.getElementById('level');
const rows = Array.from(document.querySelectorAll('#rows tbody tr'));
function applyFilter() {{
  const q = filter.value.toLowerCase();
  const mode = level.value;
  rows.forEach(row => {{
    const text = row.textContent.toLowerCase();
    const turns = Number(row.children[5].textContent || '0');
    let ok = !q || text.includes(q);
    if (mode !== 'all') {{
      if (mode === 'multi') ok = ok && turns > 1;
      else ok = ok && row.classList.contains(mode);
    }}
    row.classList.toggle('hidden', !ok);
  }});
}}
filter.addEventListener('input', applyFilter);
level.addEventListener('change', applyFilter);
</script></body></html>
"""
    Path(args.out).write_text(doc)
    print(args.out)


if __name__ == "__main__":
    main()
