#!/usr/bin/env python3
"""Classify reference-TV speech turns without retaining source transcript."""
from __future__ import annotations

import json
from collections import Counter

from openai import OpenAI

from benchmark_tv_commentary import (
    BASE,
    MEDIA_ID,
    SOURCE_PAGE,
    WINDOWS,
    load_reference,
    merged_turns,
)
from tennis_common import (
    CONFIG,
    assert_football_idle,
    load_env,
    require_env,
)

OUTPUT = BASE / "benchmarks" / "wta_tv_semantics.json"
CATEGORIES = (
    "score_or_server",
    "point_reaction_or_outcome",
    "tactics_or_pattern",
    "technique_or_shot",
    "player_background",
    "match_narrative_or_stakes",
    "conditions_or_venue",
    "banter_or_other",
    "court_official_or_player",
)
SYSTEM = """Classify speech turns from a professional tennis television
broadcast. Assign exactly one primary category to every indexed turn.

Categories:
- score_or_server: commentator states score, server, game, set, or pressure score
- point_reaction_or_outcome: commentator reacts to or explains the just-finished point
- tactics_or_pattern: strategic choices, recurring patterns, positioning, adjustments
- technique_or_shot: mechanics, stroke/serve type, execution, technical quality
- player_background: biography, career, family, prior results, factual anecdotes
- match_narrative_or_stakes: momentum, form today, significance, expectations
- conditions_or_venue: court, weather, crowd, tournament or venue context
- banter_or_other: commentary that fits none of the above
- court_official_or_player: umpire/line-call/player audio, such as isolated
  score calls, 'out', 'ready', or 'play'; do not use this for commentator
  discussion of score

Return exactly a JSON array with one object per input turn:
{"index": integer, "category": one exact category string, "reason": "brief"}.
Do not quote or reproduce the source turn in the reason."""


def parse(value: str, count: int) -> list[dict]:
    clean = value.strip()
    if clean.startswith("```"):
        clean = clean.removeprefix("```json").removeprefix("```")
        clean = clean.removesuffix("```").strip()
    rows = json.loads(clean)
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError("classifier returned wrong row count")
    indexes = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("classifier row is not an object")
        index = row.get("index")
        category = row.get("category")
        reason = row.get("reason")
        if not isinstance(index, int) or not 0 <= index < count:
            raise ValueError("classifier index is invalid")
        if category not in CATEGORIES:
            raise ValueError(f"classifier category is invalid: {category!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("classifier reason is missing")
        indexes.append(index)
    if sorted(indexes) != list(range(count)):
        raise ValueError("classifier indexes are missing or duplicated")
    return sorted(rows, key=lambda row: row["index"])


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["OPENAI_API_KEY"])
    video, _caption_text, cues = load_reference()
    turns = []
    for window in WINDOWS:
        for turn in merged_turns(
            cues,
            float(window["start_s"]),
            float(window["end_s"]),
        ):
            turns.append(
                {
                    "index": len(turns),
                    "window": window["label"],
                    **turn,
                }
            )
    input_rows = [
        {
            "index": row["index"],
            "window": row["window"],
            "start_s": round(row["start_s"], 3),
            "text": row["text"],
        }
        for row in turns
    ]
    attempts = []
    client = OpenAI()
    for attempt in range(1, 4):
        assert_football_idle()
        response = client.responses.create(
            model=CONFIG["models"]["commentary"],
            instructions=SYSTEM,
            input=json.dumps(input_rows, ensure_ascii=False),
            max_output_tokens=max(4000, len(turns) * 80),
        )
        attempts.append(parse(response.output_text or "", len(turns)))

    consensus = []
    for index, turn in enumerate(turns):
        votes = Counter(
            attempt[index]["category"]
            for attempt in attempts
        )
        category, count = votes.most_common(1)[0]
        consensus.append(
            {
                "index": index,
                "window": turn["window"],
                "start_s": round(turn["start_s"], 3),
                "end_s": round(turn["end_s"], 3),
                "category": category if count >= 2 else "uncertain",
                "agreement": count,
            }
        )

    counts = Counter(row["category"] for row in consensus)
    commentary = [
        row
        for row in consensus
        if row["category"] not in {"court_official_or_player", "uncertain"}
    ]
    result = {
        "source": {
            "publisher": "WTA",
            "page": SOURCE_PAGE,
            "media_id": MEDIA_ID,
            "name": video.get("name"),
        },
        "method": {
            "sampled_windows": list(WINDOWS),
            "classifier_attempts": 3,
            "consensus": "at least two of three identical primary categories",
            "transcript_retention": "none",
        },
        "turns": {
            "all_captioned": len(consensus),
            "commentator_consensus": len(commentary),
            "court_official_or_player": counts["court_official_or_player"],
            "uncertain": counts["uncertain"],
            "commentator_turns_per_minute": round(len(commentary) / 15, 3),
        },
        "commentator_category_counts": {
            category: counts[category]
            for category in CATEGORIES
            if category != "court_official_or_player"
        },
        "consensus_rows": consensus,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({key: value for key, value in result.items() if key != "consensus_rows"}, indent=2))


if __name__ == "__main__":
    main()
