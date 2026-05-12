#!/usr/bin/env python3
"""Convert a Speechmatics json-v2 transcript into the eval `turns.json`
schema used alongside `deepgram/turns.json` and `soniox/turns.json`.

Output (per turn):
    provider           "speechmatics"
    id                 1-based sequence
    speaker            0-based int derived from Speechmatics "S1", "S2", ...
    start              float, seconds into audio
    end                float, seconds into audio
    source_utc         float, epoch seconds when this audio occurred at the source
    source_utc_iso     ISO Z timestamp matching source_utc
    matched_log_play_at        float = source_utc + video_delay
    matched_log_play_at_iso    ISO Z timestamp matching matched_log_play_at
    text               concatenated, punctuated text of the turn
    confidence         mean word confidence
    words[]            list of {word, start, end, confidence, speaker,
                                punctuated_word}

A new turn is started when the speaker label changes OR when the inter-word
silence exceeds `--gap-seconds` (default 1.5).

Usage:
    python3 tools/speechmatics_to_turns.py \\
        --raw .../speechmatics/raw.json \\
        --meta .../extract_meta.json \\
        --out-json .../speechmatics/turns.json \\
        --out-md   .../speechmatics/turns.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
from pathlib import Path


def iso_z(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:6] + "Z"


def speaker_to_int(label: str | None) -> int:
    if not label:
        return -1
    if label.startswith("S") and label[1:].isdigit():
        # Speechmatics labels are 1-based (S1, S2, ...) — normalise to 0-based.
        return int(label[1:]) - 1
    # UU = "unknown unknown" or similar; keep as -1 sentinel.
    return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="speechmatics raw.json (json-v2)")
    ap.add_argument("--meta", required=True, help="extract_meta.json")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--gap-seconds", type=float, default=1.5,
                    help="Start a new turn when silence between words exceeds this")
    args = ap.parse_args()

    raw = json.load(open(args.raw))
    meta = json.load(open(args.meta))
    source_start = float(meta["source_start_epoch_utc"])
    video_delay = float(meta.get("video_delay_seconds", 14.0))

    # Build a flat list of word-like items (skip standalone entity rollups since
    # we already get their spoken_form entries; if not, fall back to entity).
    flat: list[dict] = []
    for r in raw.get("results", []):
        rtype = r.get("type")
        if rtype not in ("word", "punctuation"):
            # entity: include only if it has no spoken_form expansion (rare)
            if rtype == "entity" and not r.get("spoken_form"):
                pass
            else:
                continue
        alt = (r.get("alternatives") or [{}])[0]
        flat.append({
            "type": rtype,
            "start": float(r.get("start_time", 0.0)),
            "end": float(r.get("end_time", 0.0)),
            "content": alt.get("content", ""),
            "speaker": alt.get("speaker"),
            "confidence": float(alt.get("confidence", 0.0)),
            "attaches_to": r.get("attaches_to"),
            "is_eos": bool(r.get("is_eos", False)),
        })

    # Group into turns
    turns: list[dict] = []
    current_words: list[dict] = []
    current_speaker: int = -1
    last_word_end: float = 0.0

    def emit_turn():
        nonlocal current_words, current_speaker
        if not current_words:
            return
        words_only = [w for w in current_words if w["type"] == "word"]
        # Build text by walking entries: words separated by spaces, punctuation
        # attached to the previous word.
        text_parts: list[str] = []
        for w in current_words:
            if w["type"] == "punctuation":
                if text_parts:
                    text_parts[-1] = text_parts[-1] + w["content"]
                else:
                    text_parts.append(w["content"])
            else:
                text_parts.append(w["content"])
        text = " ".join(text_parts).strip()
        if not words_only or not text:
            current_words = []
            return
        start = words_only[0]["start"]
        end = words_only[-1]["end"]
        confs = [w["confidence"] for w in words_only if w["confidence"] > 0]
        mean_conf = statistics.fmean(confs) if confs else 0.0
        source_utc = source_start + start
        matched = source_utc + video_delay
        # Words array in Deepgram-style shape
        words_arr: list[dict] = []
        i = 0
        # rebuild punctuated_word per word by attaching punctuation that follows it
        for idx, w in enumerate(current_words):
            if w["type"] != "word":
                continue
            punctuated = w["content"]
            # peek ahead until next word — accumulate punctuation
            j = idx + 1
            while j < len(current_words) and current_words[j]["type"] == "punctuation":
                punctuated = punctuated + current_words[j]["content"]
                j += 1
            words_arr.append({
                "word": w["content"],
                "start": w["start"],
                "end": w["end"],
                "confidence": w["confidence"],
                "speaker": speaker_to_int(w["speaker"]),
                "punctuated_word": punctuated,
            })
        turn_id = len(turns) + 1
        turns.append({
            "provider": "speechmatics",
            "id": turn_id,
            "speaker": current_speaker,
            "start": start,
            "end": end,
            "source_utc": source_utc,
            "source_utc_iso": iso_z(source_utc),
            "matched_log_play_at": matched,
            "matched_log_play_at_iso": iso_z(matched),
            "text": text,
            "confidence": round(mean_conf, 6),
            "words": words_arr,
        })
        current_words = []

    for w in flat:
        spk = speaker_to_int(w["speaker"])
        gap = (w["start"] - last_word_end) if last_word_end else 0.0
        # Decide whether to start a new turn before adding this word.
        if current_words:
            should_break = False
            if w["type"] == "word":
                if spk != current_speaker and spk != -1:
                    should_break = True
                elif gap >= args.gap_seconds:
                    should_break = True
            if should_break:
                emit_turn()
        # Set current speaker once we know one
        if not current_words and w["type"] == "word":
            current_speaker = spk if spk != -1 else current_speaker
        current_words.append(w)
        if w["type"] == "word":
            last_word_end = w["end"]
    emit_turn()

    # Write outputs
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    json.dump(turns, open(args.out_json, "w"), ensure_ascii=False, indent=2)

    # Markdown rendering matches the Deepgram turns.md style.
    md_lines = ["# Speechmatics Batch Diarized Turns", ""]
    for t in turns:
        md_lines.append(
            f"- `{t['matched_log_play_at_iso']}` "
            f"+{t['start']:.2f}-{t['end']:.2f}s "
            f"S{t['speaker']}: {t['text']}")
    Path(args.out_md).write_text("\n".join(md_lines) + "\n")

    print(f"wrote {args.out_json} ({len(turns)} turns)")
    print(f"wrote {args.out_md}")


if __name__ == "__main__":
    main()
