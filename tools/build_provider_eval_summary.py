#!/usr/bin/env python3
"""Summarize one live/demo-live run for provider comparison.

This is intentionally provider-neutral. It reads the existing per-run JSONL logs
and emits timing/outcome metrics that are comparable across the current
STT->translation->TTS pipeline and future voice-to-voice provider adapters.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.live_translation_provider import PIPELINE_STT_TRANSLATE_TTS, provider_display_name


def _read_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _percentile(values, pct):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    idx = min(len(values) - 1, max(0, math.ceil(len(values) * pct) - 1))
    return values[idx]


def _round(value, ndigits=1):
    if value is None:
        return None
    return round(value, ndigits)


def _summarize_lang(path: Path):
    header = {}
    rows = []
    for row in _read_jsonl(path) or []:
        if row.get("type") == "header":
            header = row
        elif row.get("type") == "utterance":
            rows.append(row)

    statuses = Counter(row.get("status") for row in rows)
    total = len(rows)
    fully_played = statuses["played"]
    interrupted = statuses["interrupted"]
    skipped = (
        statuses["dropped"]
        + statuses["replaced"]
        + statuses["suppressed"]
    )
    outcome_total = fully_played + interrupted + skipped

    metrics = defaultdict(list)
    for row in rows:
        for key in (
            "trans_ms",
            "tts_ms",
            "queue_wait_ms",
            "start_lag_ms",
            "intended_skew_ms",
            "play_duration_ms",
            "local_speed_factor",
            "v2v_first_audio_ms",
            "v2v_buffered_at_play_start_ms",
            "v2v_total_audio_ms",
            "v2v_underruns",
        ):
            value = row.get(key)
            if isinstance(value, (int, float)):
                metrics[key].append(value)
        if isinstance(row.get("ready_at"), (int, float)) and isinstance(row.get("play_at"), (int, float)):
            metrics["ready_vs_play_ms"].append((row["ready_at"] - row["play_at"]) * 1000)
        if isinstance(row.get("play_started_at"), (int, float)) and isinstance(row.get("occurred_at"), (int, float)):
            metrics["e2e_play_latency_ms"].append((row["play_started_at"] - row["occurred_at"]) * 1000)

    guard = Counter(row.get("translation_guard_status") for row in rows if row.get("translation_guard_status"))
    model = Counter(row.get("translation_model_used") for row in rows if row.get("translation_model_used"))
    fallback = Counter(row.get("translation_fallback_reason") for row in rows if row.get("translation_fallback_reason"))
    provider_errors = Counter(row.get("provider_error") for row in rows if row.get("provider_error"))

    summary = {
        "language": path.stem,
        "header": {
            "pipeline_mode": header.get("pipeline_mode"),
            "speech_translation_provider": header.get("speech_translation_provider"),
            "pipeline_label": header.get("pipeline_label"),
            "stt_provider": header.get("stt_provider"),
            "voice_id": header.get("voice_id"),
        },
        "total": total,
        "played": fully_played,
        "interrupted": interrupted,
        "dropped": statuses["dropped"],
        "replaced": statuses["replaced"],
        "suppressed": statuses["suppressed"],
        "fully_played_pct": round(fully_played / outcome_total * 100, 1) if outcome_total else None,
        "guards": dict(guard),
        "models": dict(model),
        "fallback_reasons": dict(fallback),
        "provider_errors": dict(provider_errors),
        "metrics": {},
    }
    for key, values in metrics.items():
        summary["metrics"][key] = {
            "p50": _round(_percentile(values, 0.50)),
            "p90": _round(_percentile(values, 0.90)),
            "max": _round(max(values) if values else None),
        }
    return summary


def build_summary(run_dir: Path):
    stt_header = {}
    stt_utterances = 0
    speakers = Counter()
    for row in _read_jsonl(run_dir / "stt.jsonl") or []:
        if row.get("type") == "header":
            stt_header = row
        elif row.get("type") == "utterance":
            stt_utterances += 1
            speakers[row.get("speaker")] += 1

    languages = {}
    for path in sorted(run_dir.glob("*.jsonl")):
        if path.name == "stt.jsonl":
            continue
        languages[path.stem] = _summarize_lang(path)

    return {
        "run_dir": str(run_dir),
        "match_id": stt_header.get("match_id"),
        "started_at": stt_header.get("started_at"),
        "pipeline_mode": stt_header.get("pipeline_mode") or PIPELINE_STT_TRANSLATE_TTS,
        "speech_translation_provider": stt_header.get("speech_translation_provider") or "",
        "pipeline_label": stt_header.get("pipeline_label") or provider_display_name(
            stt_header.get("pipeline_mode") or PIPELINE_STT_TRANSLATE_TTS,
            stt_header.get("speech_translation_provider") or "",
        ),
        "stt_provider": stt_header.get("stt_provider"),
        "video_delay": stt_header.get("video_delay"),
        "stt_utterances": stt_utterances,
        "speakers": {str(k): v for k, v in speakers.items()},
        "languages": languages,
    }


def write_markdown(summary: dict, path: Path):
    lines = []
    lines.append(f"# Provider Eval Summary — {summary.get('match_id') or 'unknown'}")
    lines.append("")
    lines.append(f"- Run dir: `{summary['run_dir']}`")
    lines.append(f"- Started: `{summary.get('started_at')}`")
    lines.append(f"- Pipeline: `{summary.get('pipeline_label') or summary.get('pipeline_mode')}`")
    lines.append(f"- STT provider: `{summary.get('stt_provider')}`")
    lines.append(f"- Video delay: `{summary.get('video_delay')}`")
    lines.append(f"- STT utterances: `{summary.get('stt_utterances')}`")
    lines.append("")
    is_v2v = summary.get("pipeline_mode") == "voice_to_voice"
    if is_v2v:
        lines.append("| Lang | Fully played | Dropped | Interrupted | v2v first audio | buffered @ start | v2v audio | underruns |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    else:
        lines.append("| Lang | Fully played | Dropped | Interrupted | trans p50/p90 | tts p50/p90 | start lag p90 | ready-play p90 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for lang, data in summary["languages"].items():
        metrics = data.get("metrics", {})
        if is_v2v:
            first_audio = metrics.get("v2v_first_audio_ms", {})
            buffered = metrics.get("v2v_buffered_at_play_start_ms", {})
            total_audio = metrics.get("v2v_total_audio_ms", {})
            underruns = metrics.get("v2v_underruns", {})
            lines.append(
                f"| {lang.upper()} | {data.get('fully_played_pct')}% | "
                f"{data.get('dropped')} | {data.get('interrupted')} | "
                f"{first_audio.get('p50')} ms | {buffered.get('p50')} ms | "
                f"{total_audio.get('p50')} ms | {underruns.get('max')} |"
            )
        else:
            trans = metrics.get("trans_ms", {})
            tts = metrics.get("tts_ms", {})
            lag = metrics.get("start_lag_ms", {})
            ready = metrics.get("ready_vs_play_ms", {})
            lines.append(
                f"| {lang.upper()} | {data.get('fully_played_pct')}% | "
                f"{data.get('dropped')} | {data.get('interrupted')} | "
                f"{trans.get('p50')}/{trans.get('p90')} | "
                f"{tts.get('p50')}/{tts.get('p90')} | "
                f"{lag.get('p90')} | {ready.get('p90')} |"
            )
    path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", help="match id under match_data/")
    parser.add_argument("--run", help="run id under match_data/{match}/runs/")
    parser.add_argument("--run-dir", help="explicit run directory")
    parser.add_argument("--out-dir", help="output directory; defaults to run dir")
    args = parser.parse_args()

    if args.run_dir:
        run_dir = Path(args.run_dir)
    elif args.match and args.run:
        run_dir = Path("match_data") / args.match / "runs" / args.run
    else:
        parser.error("provide --run-dir or both --match and --run")

    summary = build_summary(run_dir)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "provider_eval_summary.json"
    md_path = out_dir / "provider_eval_summary.md"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    write_markdown(summary, md_path)
    print(md_path.read_text())


if __name__ == "__main__":
    main()
