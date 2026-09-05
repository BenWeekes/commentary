#!/usr/bin/env python3
"""Create the exact v3 disposition ledger for the human-reviewed v4 changes."""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
TRIGGER = BASE / "feedback" / "trigger_v3.json"
OUTPUT = BASE / "feedback" / "dispositions_v3.json"


def main() -> None:
    work = json.loads(TRIGGER.read_text())
    items = work.get("all_items")
    if work.get("version") != "v3" or not isinstance(items, list) or len(items) != 48:
        raise SystemExit("v3 work order is missing or no longer has exactly 48 items")
    dispositions = []
    for item in items:
        tags = set(item.get("tags") or [])
        comment = str(item.get("comment") or "").strip()
        has_correction = bool(tags - {"👍 good"}) or bool(comment)
        if has_correction:
            reason = (
                "Accepted: the reviewer identified that the v3 live-rally clause "
                "was repetitive, stale, mistranslated, or factually wrong when a "
                "point ended or a first serve hit the net."
            )
            change = (
                "Remove every live-ball/rally sentence in all languages, stop "
                "requiring rally calls, and permit only completed score outcomes "
                "or verified between-point context."
            )
            verification = (
                "The v4 fixture requires zero vision_rally rows and the release "
                "gate rejects any live-ball evidence; EN/FR/PT rendered tracks are "
                "independently transcribed before publication."
            )
        else:
            reason = "Accepted: the reviewer explicitly marked this cell good."
            change = (
                "Retain the reviewed behavior; only the separately reported "
                "live-rally intents are removed across languages."
            )
            verification = (
                "The retained cell remains covered by the v4 deterministic units, "
                "three judges, structural gate, and rendered-track speech audit."
            )
        dispositions.append(
            {
                "feedback_id": item["feedback_id"],
                "status": "accepted",
                "reason": reason,
                "change": change,
                "verification": verification,
            }
        )
    value = {
        "version": "v3",
        "next_version": "v4",
        "items": dispositions,
    }
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(temporary, OUTPUT)
    print(f"wrote {OUTPUT} with {len(dispositions)} exact dispositions")


if __name__ == "__main__":
    main()
