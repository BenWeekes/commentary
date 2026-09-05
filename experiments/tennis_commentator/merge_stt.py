#!/usr/bin/env python3
"""Merge STT providers while retaining provenance and removing close duplicates."""
from __future__ import annotations

import json
import re
from collections import Counter

from tennis_common import SHARED_ARTIFACTS as ARTIFACTS, read_jsonl


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def similar(left: dict, right: dict) -> bool:
    overlap = min(float(left["end_s"]), float(right["end_s"])) - max(
        float(left["video_time_s"]), float(right["video_time_s"])
    )
    if overlap < -1.0:
        return False
    a, b = normalized(left["text"]), normalized(right["text"])
    return bool(a and b and (a in b or b in a))


def rejection_reason(row: dict, frequencies: Counter) -> str | None:
    text = normalized(str(row.get("text") or ""))
    if not text:
        return "empty"
    if "transcribe only audible speech" in text:
        return "prompt_echo"
    if frequencies[text] > 5:
        return "pathological_repetition"
    if row.get("provider") == "whisper" and float(row.get("conf", 0)) < 0.5:
        return "whisper_low_confidence"
    return None


def main() -> None:
    candidates = read_jsonl(ARTIFACTS / "stt.jsonl") + read_jsonl(
        ARTIFACTS / "stt_whisper.jsonl"
    )
    frequencies = Counter(normalized(str(row.get("text") or "")) for row in candidates)
    rejected = []
    usable = []
    for row in candidates:
        reason = rejection_reason(row, frequencies)
        if reason:
            rejected.append({**row, "rejection_reason": reason})
        else:
            usable.append(row)
    candidates = usable
    candidates.sort(key=lambda row: (float(row["video_time_s"]), -float(row.get("conf", 0))))
    kept = []
    for row in candidates:
        duplicate = next((item for item in kept if similar(item, row)), None)
        if duplicate is None:
            kept.append(row)
        elif float(row.get("conf", 0)) > float(duplicate.get("conf", 0)):
            kept[kept.index(duplicate)] = row
    kept.sort(key=lambda row: float(row["video_time_s"]))
    out = ARTIFACTS / "stt_merged.jsonl"
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept))
    rejected_out = ARTIFACTS / "stt_rejected.jsonl"
    rejected_out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rejected)
    )
    print(
        f"wrote {out} ({len(kept)} merged utterances); "
        f"{len(rejected)} rejected -> {rejected_out}"
    )


if __name__ == "__main__":
    main()
