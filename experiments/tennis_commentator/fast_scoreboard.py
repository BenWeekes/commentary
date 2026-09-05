#!/usr/bin/env python3
"""Observe the fixed Cary score graphic locally for the two-second profile.

The observer is deliberately narrow: it reads the stable point cells from a
five-frame-per-second grayscale crop, learns each new glyph only when it is the
unique legal next tennis value, and requires two agreeing frames.  It does not
claim to be a general OCR engine.  The build cross-checks its complete state
sequence against the independently accepted v1 vision tracker and fails closed
if they differ.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

from run_commentary import _score_from_detection
from score_tracker import INITIAL, Score, ScoreTracker
from tennis_common import (
    CLIP,
    CONFIG,
    OUTPUT_ARTIFACTS,
    SHARED_ARTIFACTS,
    assert_football_idle,
    read_jsonl,
)

WIDTH = 224
HEIGHT = 60
SCORE_CROP = "crop=224:60:64:438"
POINT_BOXES = {
    "far": (188, 6, 32, 22),
    "near": (188, 34, 32, 22),
}
GLYPH_DISTANCE = 0.03
POINT_SEQUENCE = {"0": "15", "15": "30", "30": "40"}


def crop(frame: bytes, box: tuple[int, int, int, int]) -> list[int]:
    x, y, width, height = box
    return [
        frame[(y + row) * WIDTH + x + column]
        for row in range(height)
        for column in range(width)
    ]


def signature(pixels: list[int]) -> tuple[bool, ...]:
    return tuple(value > 120 for value in pixels)


def distance(left: tuple[bool, ...], right: tuple[bool, ...]) -> float:
    return sum(a != b for a, b in zip(left, right)) / len(left)


def panel_visible(frame: bytes) -> bool:
    panel = [frame[y * WIDTH + x] for y in range(HEIGHT) for x in range(145)]
    return sum(value < 60 for value in panel) / len(panel) >= 0.70


def point_cells_absent(frame: bytes) -> bool:
    return all(
        sum(crop(frame, box)) / (box[2] * box[3]) > 90
        for box in POINT_BOXES.values()
    )


def with_point(score: Score, side: str, value: str) -> Score:
    values = score.json()
    values[f"{side}_points"] = value
    return Score(**values)


def complete_game(score: Score) -> Score:
    if score.far_points == "40" and score.near_points != "40":
        winner = "far"
    elif score.near_points == "40" and score.far_points != "40":
        winner = "near"
    else:
        raise RuntimeError(f"score-cell disappearance is ambiguous at {score}")
    values = score.json()
    values[f"{winner}_games"] += 1
    values["far_points"] = values["near_points"] = "0"
    values["server"] = "far" if score.server == "near" else "near"
    return Score(**values)


def baseline_events() -> list[Score]:
    tracker = ScoreTracker(INITIAL)
    accepted = []
    for source in read_jsonl(SHARED_ARTIFACTS / "detections.jsonl"):
        score = _score_from_detection(source["detection"], tracker.current)
        result = tracker.observe(
            score,
            float((source["detection"].get("scoreboard") or {}).get("confidence", 0)),
        )
        if result.get("changed"):
            accepted.append(tracker.current)
    return accepted


def observe(raw: bytes, fps: int, confirmation_frames: int) -> list[dict]:
    frame_size = WIDTH * HEIGHT
    if len(raw) % frame_size:
        raise RuntimeError("fast scoreboard decoder returned a partial frame")
    frames = [raw[offset:offset + frame_size] for offset in range(0, len(raw), frame_size)]
    if len(frames) < 300 * fps - 2:
        raise RuntimeError(f"fast scoreboard decoder returned only {len(frames)} frames")

    first = next(frame for frame in frames if panel_visible(frame))
    templates = {
        side: {"0": signature(crop(first, box))}
        for side, box in POINT_BOXES.items()
    }
    current = INITIAL
    pending: dict[str, tuple[str, tuple[bool, ...], int] | None] = {
        "far": None,
        "near": None,
    }
    game_frames = 0
    awaiting_reset = False
    panel_went_hidden = False
    reset_frames = 0
    events = []

    def emit(at: float, previous: Score, score: Score, evidence: str) -> None:
        events.append(
            {
                "video_time_s": round(at, 3),
                "previous_score": previous.json(),
                "score": score.json(),
                "evidence": evidence,
            }
        )

    for index, frame in enumerate(frames):
        at = index / fps
        visible = panel_visible(frame)
        if not visible:
            panel_went_hidden = panel_went_hidden or awaiting_reset
            pending = {"far": None, "near": None}
            game_frames = 0
            continue

        glyphs = {
            side: signature(crop(frame, box))
            for side, box in POINT_BOXES.items()
        }
        if awaiting_reset:
            is_zero = all(
                distance(glyphs[side], templates[side]["0"]) < GLYPH_DISTANCE
                for side in POINT_BOXES
            )
            if panel_went_hidden and is_zero:
                reset_frames += 1
                if reset_frames >= confirmation_frames:
                    awaiting_reset = False
                    pending = {"far": None, "near": None}
            else:
                reset_frames = 0
            continue

        if point_cells_absent(frame) and (
            current.far_points == "40" or current.near_points == "40"
        ):
            game_frames += 1
            if game_frames >= confirmation_frames:
                previous = current
                current = complete_game(current)
                emit(
                    at,
                    previous,
                    current,
                    "two_frame_score_cell_disappearance_after_game_point",
                )
                awaiting_reset = True
                panel_went_hidden = False
                reset_frames = 0
                pending = {"far": None, "near": None}
            continue
        game_frames = 0

        for side, glyph in glyphs.items():
            current_value = getattr(current, f"{side}_points")
            current_template = templates[side].get(current_value)
            if current_template and distance(glyph, current_template) < GLYPH_DISTANCE:
                pending[side] = None
                continue
            next_value = POINT_SEQUENCE.get(current_value)
            if not next_value:
                pending[side] = None
                continue
            known = templates[side].get(next_value)
            if known is not None and distance(glyph, known) >= GLYPH_DISTANCE:
                pending[side] = None
                continue
            prior = pending[side]
            if (
                prior is not None
                and prior[0] == next_value
                and distance(prior[1], glyph) < GLYPH_DISTANCE
            ):
                count = prior[2] + 1
            else:
                count = 1
            pending[side] = (next_value, glyph, count)
            if count < confirmation_frames:
                continue
            previous = current
            current = with_point(current, side, next_value)
            templates[side].setdefault(next_value, glyph)
            emit(at, previous, current, "two_agreeing_fixed_layout_point_glyphs")
            pending = {"far": None, "near": None}
            break
    return events


def main() -> None:
    assert_football_idle()
    fps = int(CONFIG["timing"]["fast_scoreboard_fps"])
    confirmation_frames = int(
        CONFIG["timing"]["fast_scoreboard_confirmation_frames"]
    )
    started = time.monotonic()
    process = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(CLIP),
            "-vf", f"fps={fps},scale=960:540,{SCORE_CROP},format=gray",
            "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    )
    elapsed = time.monotonic() - started
    frame_count = len(process.stdout) // (WIDTH * HEIGHT)
    per_frame = elapsed / max(1, frame_count)
    readiness = confirmation_frames / fps + per_frame
    events = observe(process.stdout, fps, confirmation_frames)
    expected = baseline_events()
    actual_states = [Score(**row["score"]) for row in events]
    if actual_states != expected:
        raise SystemExit(
            "fast scoreboard state sequence differs from accepted independent tracker:\n"
            f"expected={[item.json() for item in expected]}\n"
            f"actual={[item.json() for item in actual_states]}"
        )
    if not math.isfinite(readiness) or readiness >= 1.0:
        raise SystemExit(f"fast scoreboard readiness is not safely sub-second: {readiness}")
    for row in events:
        row["latency_s"] = round(readiness, 3)
        row["observer"] = "local_fixed_layout_scoreboard_v1"
        row["confirmation_frames"] = confirmation_frames
        row["fps"] = fps

    OUTPUT_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl"
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events)
    )
    metrics = {
        "observer": "local_fixed_layout_scoreboard_v1",
        "fps": fps,
        "confirmation_frames": confirmation_frames,
        "frames": frame_count,
        "events": len(events),
        "decode_and_observe_wall_s": round(elapsed, 4),
        "per_frame_processing_s": round(per_frame, 6),
        "worst_case_readiness_s": round(readiness, 3),
        "cross_check": "exact state-sequence match with independent accepted v1 tracker",
        "scope": "fixed Cary broadcast score graphic; fail closed outside this layout",
    }
    (OUTPUT_ARTIFACTS / "fast_scoreboard_metrics.json").write_text(
        json.dumps(metrics, indent=2)
    )
    print(json.dumps(metrics, indent=2))
    for row in events:
        print(f"[{row['video_time_s']:6.1f}s] {row['score']}")


if __name__ == "__main__":
    main()
