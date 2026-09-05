#!/usr/bin/env python3
"""Require an explicit disposition for every item in the previous closed round."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
FEEDBACK = BASE / "feedback"
VALID = {"accepted", "rejected", "duplicate"}
FIELDS = ("status", "reason", "change", "verification")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", required=True)
    args = parser.parse_args()
    trigger = FEEDBACK / f"trigger_{args.round}.json"
    dispositions = FEEDBACK / f"dispositions_{args.round}.json"
    if not trigger.exists():
        raise SystemExit(f"missing closed-round work order: {trigger}")
    if not dispositions.exists():
        raise SystemExit(f"missing dispositions: {dispositions}")
    work = json.loads(trigger.read_text())
    value = json.loads(dispositions.read_text())
    items = value.get("items")
    if not isinstance(items, list):
        raise SystemExit("dispositions items[] missing")
    expected_items = work.get("all_items")
    if not isinstance(expected_items, list):
        raise SystemExit("work order is missing all_items[]")
    expected_ids = {
        item.get("feedback_id")
        for item in expected_items
        if isinstance(item, dict) and isinstance(item.get("feedback_id"), str)
    }
    if len(expected_ids) != len(expected_items):
        raise SystemExit("work order has missing or duplicate feedback IDs")
    provided_ids = [
        item.get("feedback_id") for item in items if isinstance(item, dict)
    ]
    if len(provided_ids) != len(set(provided_ids)):
        raise SystemExit("dispositions contain duplicate feedback IDs")
    if set(provided_ids) != expected_ids:
        missing = sorted(expected_ids - set(provided_ids))
        extra = sorted(set(provided_ids) - expected_ids)
        raise SystemExit(f"disposition IDs do not match feedback; missing={missing}, extra={extra}")
    failures = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append(f"{index}: not an object")
            continue
        if item.get("status") not in VALID:
            failures.append(f"{index}: invalid status")
        for field in FIELDS[1:]:
            if not isinstance(item.get(field), str) or not item[field].strip():
                failures.append(f"{index}: missing {field}")
    if failures:
        raise SystemExit("unaddressed review points:\n" + "\n".join(failures))
    print(f"{args.round}: all {len(expected_ids)} review points have explicit dispositions")


if __name__ == "__main__":
    main()
