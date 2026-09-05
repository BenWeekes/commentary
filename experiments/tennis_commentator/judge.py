#!/usr/bin/env python3
"""Grounded hallucination judge with strict numeric 0/1 parsing."""
from __future__ import annotations

import argparse
import json
import re

from openai import OpenAI

from tennis_common import ARTIFACTS, CONFIG, assert_football_idle, load_env, read_jsonl, require_env

SYSTEM = """Audit live tennis commentary against the supplied grounding bundle.
For every item, verify the named point winner, score, server, streak, game
outcome, pressure, and any background fact. A claim is supported only when it
follows from the previous/current accepted trackers and the supplied tennis
rules, or from explicit vision/pre-match evidence. The structured intent is an
auditable derivation, not independent evidence. Style is not a hallucination.
Return exactly a JSON array with one object per input item:
{"index": integer, "hallucination_likely": 0 or 1, "reason": "short"}.
Do not omit items and do not use booleans or strings for the numeric field."""


def parse(text: str, count: int) -> list[dict]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean)
    value = json.loads(clean)
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("judge output has wrong item count")
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("judge row is not an object")
        index = row.get("index")
        likely = row.get("hallucination_likely")
        if not isinstance(index, int) or index in seen or not 0 <= index < count:
            raise ValueError("judge index is missing, duplicate, or invalid")
        if type(likely) is not int or likely not in (0, 1):
            raise ValueError("hallucination_likely must be numeric 0 or 1")
        if not isinstance(row.get("reason"), str):
            raise ValueError("judge reason is missing")
        seen.add(index)
    if seen != set(range(count)):
        raise ValueError("judge indexes are incomplete")
    return sorted(value, key=lambda item: item["index"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", type=int, required=True, choices=(1, 2, 3))
    args = parser.parse_args()
    assert_football_idle()
    load_env()
    require_env(["OPENAI_API_KEY"])
    rows = [
        row for row in read_jsonl(ARTIFACTS / f"commentary_attempt_{args.attempt}.jsonl")
        if not row.get("dropped")
    ]
    bundle = [
        {
            "index": index,
            "text": row["text"],
            "source": row.get("src"),
            "vision": row.get("vision"),
            "phase": row.get("phase"),
            "point": row.get("point"),
            "structured_intent": row.get("intent"),
            "emission_policy": row.get("policy"),
            "previous_tracker": row.get("previous_tracker"),
            "accepted_tracker": row.get("tracker"),
            "tracker_identity": (
                "The tracker labels 'far' and 'near' are immutable scoreboard "
                "row/player IDs: far=Daniil Glinka and near=Aidan Mayo. They "
                "are not physical court ends after the changeover. The server "
                "field uses those stable IDs; after the opening game, far "
                "still means Glinka even though Glinka is physically near."
            ),
            "verified_match_context": (
                "At clip start Mayo serves. Glinka is left-handed. The players "
                "change court ends after the first game."
            ),
            "tennis_scoring_rules": (
                "At 40-love the leading player has three game points; at "
                "40-15 the leading player has two; at 40-30 or advantage the "
                "leader has one. A legal point-score transition identifies "
                "the point winner. A player's game counter rising by one "
                "means that player won the completed game. Winning the game "
                "while serving is a hold; winning while receiving is a break. "
                "Game score and point score are separate: for example, at "
                "0-1 games and 0-0 points, 'love-all' correctly describes the "
                "points in the current game."
            ),
            "verified_pre_match": (
                "This is a Cary round-of-32 match. Glinka is the third seed. "
                "Mayo entered with a protected ranking. "
                "Mayo won the 2024 Drummondville Challenger; "
                "Glinka won the 2025 Drummondville Challenger."
                if str(row.get("src", "")).startswith("pre_match") else None
            ),
            "verified_match_rules": (
                "Mayo served the opening game. Players change ends after the "
                "first game. Glinka is left-handed and serves the second game."
                if row.get("src") in {"changeover", "serve_context"} else None
            ),
        }
        for index, row in enumerate(rows)
    ]
    assert_football_idle()
    response = OpenAI().responses.create(
        model=CONFIG["models"]["commentary"],
        instructions=SYSTEM,
        input=json.dumps(bundle, ensure_ascii=False),
        max_output_tokens=max(400, len(bundle) * 80),
    )
    result = parse(response.output_text or "", len(bundle))
    out = ARTIFACTS / f"judge_attempt_{args.attempt}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    if any(row["hallucination_likely"] == 1 for row in result):
        raise SystemExit(f"attempt {args.attempt}: hallucination judge failed")
    print(f"wrote {out}; {len(result)} lines passed")


if __name__ == "__main__":
    main()
