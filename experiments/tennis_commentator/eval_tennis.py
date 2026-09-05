#!/usr/bin/env python3
"""Fail-closed structural and worst-of-three tennis quality gate."""
from __future__ import annotations

import json
import math
import re
import statistics
import subprocess

from tennis_common import (
    ARTIFACTS,
    CONFIG,
    DELAY_S,
    OUTPUT_ARTIFACTS,
    PROFILE,
    PipelineError,
    SHARED_ARTIFACTS,
    read_jsonl,
)
from run_commentary import background_allowed, render_score_intent
from render_tracks import VOICES, tts_duration_limit_s
from score_tracker import Score, point_winner, pressure_state, transition_type

BANNED = re.compile(r"\b(?:camera|frame|picture|on screen|we can see|visible)\b", re.I)
SERVER = re.compile(r"\bserv(?:e|es|ing)\b", re.I)
PRESSURE = re.compile(r"\b(?:game|break|set|match) points?\b", re.I)
WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def media_faults(path, *, video: bool) -> list[str]:
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(probe.stdout)
        duration = float(value["format"]["duration"])
        stream_types = {item.get("codec_type") for item in value.get("streams", [])}
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [f"media probe failed: {path}"]
    faults = []
    if not math.isfinite(duration) or abs(duration - 300.0) > 0.25:
        faults.append(f"media duration {duration:.3f}s is not 300s: {path}")
    required_streams = {"audio", "video"} if video else {"audio"}
    if not required_streams <= stream_types:
        faults.append(f"media streams {sorted(stream_types)} missing {sorted(required_streams)}: {path}")
    return faults


def evaluate(attempt: int) -> dict:
    rows = read_jsonl(ARTIFACTS / f"commentary_attempt_{attempt}.jsonl")
    judge_path = ARTIFACTS / f"judge_attempt_{attempt}.json"
    if not judge_path.exists():
        raise PipelineError(f"missing judge artifact for attempt {attempt}")
    judge = json.loads(judge_path.read_text())
    kept = [row for row in rows if not row.get("dropped")]
    faults = []
    quality = CONFIG["quality"]
    minimum_lines = int(quality["minimum_lines"])
    maximum_lines = int(quality["maximum_lines"])
    if not minimum_lines <= len(kept) <= maximum_lines:
        faults.append(
            f"kept line count {len(kept)} outside "
            f"{minimum_lines}..{maximum_lines}"
        )
    if not kept or kept[0].get("src") != "pre_match":
        faults.append("verified pre-match opener missing")
    if any(not row.get("fr") or not row.get("pt") for row in kept):
        faults.append("missing FR/PT localization")
    if any(
        not isinstance(row.get("intent"), dict)
        or not isinstance(row.get("policy"), dict)
        or row["policy"].get("decision") != "emit"
        or not row["intent"].get("evidence")
        for row in kept
    ):
        faults.append("missing or malformed structured intent/policy")
    if any(BANNED.search(row.get("text", "")) for row in kept):
        faults.append("camera/picture language present")
    normalized = [re.sub(r"\W+", " ", row.get("text", "").lower()).strip() for row in kept]
    if len(set(normalized)) != len(normalized):
        faults.append("exact commentary repetition")
    times = [0.0, *sorted(float(row["video_time_s"]) for row in kept), 300.0]
    max_gap = max(later - earlier for earlier, later in zip(times, times[1:]))
    maximum_gap = float(quality["maximum_gap_seconds"])
    if max_gap > maximum_gap:
        faults.append(
            f"maximum commentary gap {max_gap:.1f}s exceeds {maximum_gap:g}s"
        )
    coverage = {
        "server_calls": sum(bool(SERVER.search(row.get("text", ""))) for row in kept),
        "rally_calls": sum(row.get("src") == "vision_rally" for row in kept),
        "changeover_calls": sum(row.get("src") == "changeover" for row in kept),
        "service_context_calls": sum(row.get("src") == "serve_context" for row in kept),
        "outcome_calls": sum(row.get("src") == "score_tracker" for row in kept),
        "pressure_calls": sum(bool(PRESSURE.search(row.get("text", ""))) for row in kept),
        "background_calls": sum(row.get("src") == "pre_match_color" for row in kept),
    }
    required_coverage = {
        "server_calls": int(quality["minimum_server_calls"]),
        "rally_calls": int(quality["minimum_rally_calls"]),
        "changeover_calls": int(quality["minimum_changeover_calls"]),
        "service_context_calls": int(quality["minimum_service_context_calls"]),
        "outcome_calls": int(quality["minimum_outcome_calls"]),
        "pressure_calls": int(quality["minimum_pressure_calls"]),
    }
    for name, minimum in required_coverage.items():
        if coverage[name] < minimum:
            faults.append(f"{name} {coverage[name]} below {minimum}")
    maximum_background = int(quality["maximum_background_calls"])
    if coverage["background_calls"] > maximum_background:
        faults.append(
            f"background_calls {coverage['background_calls']} exceeds "
            f"{maximum_background}"
        )
    maximum_live_ball = int(quality.get("maximum_live_ball_calls", 0))
    if coverage["rally_calls"] > maximum_live_ball:
        faults.append(
            f"live-ball calls {coverage['rally_calls']} exceed "
            f"human-reviewed maximum {maximum_live_ball}"
        )
    if any("vision_live_ball" in (row.get("intent") or {}).get("evidence", []) for row in kept):
        faults.append("live-ball evidence survived the v3 review correction")

    score_rows = [row for row in kept if row.get("src") == "score_tracker"]
    for row in score_rows:
        try:
            previous = Score(**row["previous_tracker"])
            current = Score(**row["tracker"])
            intent = row["intent"]
            kind = transition_type(previous, current)
            if kind != intent.get("transition"):
                faults.append(
                    f"intent transition mismatch at {row['video_time_s']}s"
                )
            if (
                kind != "set_reset"
                and point_winner(previous, current) != intent.get("winner")
            ):
                faults.append(f"intent winner mismatch at {row['video_time_s']}s")
            rendered = render_score_intent(intent)
            if rendered != (row.get("text"), row.get("fr"), row.get("pt")):
                faults.append(
                    f"localized rendering differs from intent at "
                    f"{row['video_time_s']}s"
                )
        except (KeyError, TypeError, ValueError) as exc:
            faults.append(
                f"score intent cannot be verified at "
                f"{row.get('video_time_s')}s: {exc}"
            )

    for row in kept:
        try:
            tracker_score = Score(**row["tracker"])
            pressure = pressure_state(
                tracker_score,
                int(CONFIG["match"].get("best_of_sets", 3)),
            )
            if not pressure["supported"]:
                faults.append(
                    f"unsupported tiebreak/score state at {row['video_time_s']}s"
                )
            if row.get("src") == "pre_match_color":
                allowed, reason = background_allowed(tracker_score)
                if not allowed:
                    faults.append(
                        f"background emitted during {reason} at "
                        f"{row['video_time_s']}s"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            faults.append(
                f"tracker pressure cannot be verified at "
                f"{row.get('video_time_s')}s: {exc}"
            )

    word_counts = [len(WORD.findall(row.get("text", ""))) for row in kept]
    median_words = statistics.median(word_counts) if word_counts else 0
    p90_words = (
        sorted(word_counts)[max(0, math.ceil(len(word_counts) * 0.9) - 1)]
        if word_counts else 0
    )
    maximum_words = max(word_counts, default=0)
    if median_words < float(quality["minimum_median_words"]):
        faults.append(f"median words {median_words:g} below target")
    if median_words > float(quality["maximum_median_words"]):
        faults.append(f"median words {median_words:g} above target")
    if p90_words > int(quality["maximum_p90_words"]):
        faults.append(f"p90 words {p90_words} above target")
    if maximum_words > int(quality["maximum_words_per_line"]):
        faults.append(f"maximum words {maximum_words} above target")
    if any(type(item.get("hallucination_likely")) is not int for item in judge):
        faults.append("malformed hallucination judge schema")
    if any(item.get("hallucination_likely") == 1 for item in judge):
        faults.append("hallucination judge positive")
    if len(judge) != len(kept):
        faults.append(f"judge row count {len(judge)} != kept line count {len(kept)}")
    delay = DELAY_S
    if any(float(row.get("pipeline_latency_s", delay + 1)) > delay for row in kept):
        faults.append("kept line exceeds model pipeline delay")
    max_shift = None
    if attempt == 1:
        if any("end_to_end_latency_s" not in row for row in kept):
            faults.append("selected attempt is missing TTS end-to-end latency")
        elif any(float(row["end_to_end_latency_s"]) > delay for row in kept):
            faults.append("kept selected-attempt line exceeds end-to-end delay")
        expected_tts_mode = (
            "prewarmed_before_match"
            if CONFIG["timing"].get("prewarm_tts")
            else "just_in_time"
        )
        if any(row.get("tts_mode") != expected_tts_mode for row in kept):
            faults.append("selected attempt TTS mode differs from timing contract")
        if any(
            set((row.get("placements") or {}).keys()) != {"en", "fr", "pt"}
            for row in kept
        ):
            faults.append("selected attempt is missing language placements")
        else:
            max_shift = max(
                float(placement["shift_s"])
                for row in kept
                for placement in row["placements"].values()
            )
            if max_shift > float(quality["maximum_audio_shift_seconds"]):
                faults.append(
                    f"audio placement shift {max_shift:.3f}s exceeds target"
                )
            for row in kept:
                texts = {
                    "en": row["text"],
                    "fr": row["fr"],
                    "pt": row["pt"],
                }
                for lang, text in texts.items():
                    duration = float(row["placements"][lang]["duration_s"])
                    if duration > tts_duration_limit_s(text):
                        faults.append(
                            f"implausible {lang} TTS duration {duration:.3f}s "
                            f"at {row['video_time_s']}s"
                        )
    survival = len(kept) / max(1, len(rows))
    minimum_survival = float(quality["minimum_survival_rate"])
    if survival < minimum_survival:
        faults.append(
            f"survival {survival:.3f} below {minimum_survival:.3f}"
        )
    return {
        "attempt": attempt,
        "status": "PASS" if not faults else "FAIL",
        "candidates": len(rows),
        "kept": len(kept),
        "survival_rate": round(survival, 4),
        "max_gap_s": round(max_gap, 3),
        "median_words": median_words,
        "p90_words": p90_words,
        "maximum_words": maximum_words,
        "maximum_audio_shift_s": max_shift,
        "coverage": coverage,
        "faults": faults,
    }


def main() -> None:
    review_media = [ARTIFACTS / f"review_{lang}.mp4" for lang in ("en", "fr", "pt")]
    audio_media = [ARTIFACTS / f"ai_{lang}.wav" for lang in ("en", "fr", "pt")]
    required = [
        OUTPUT_ARTIFACTS / "input_manifest.json",
        OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl",
        OUTPUT_ARTIFACTS / "fast_scoreboard_metrics.json",
        SHARED_ARTIFACTS / "clip.mp4",
        SHARED_ARTIFACTS / "stt.jsonl",
        SHARED_ARTIFACTS / "stt_whisper.jsonl",
        SHARED_ARTIFACTS / "stt_merged.jsonl",
        SHARED_ARTIFACTS / "stt_rejected.jsonl",
        SHARED_ARTIFACTS / "detections.jsonl",
        ARTIFACTS / "tracker.jsonl",
        ARTIFACTS / "render_manifest.json",
        *audio_media,
        *review_media,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PipelineError("missing required artifacts: " + ", ".join(missing))
    detections = read_jsonl(SHARED_ARTIFACTS / "detections.jsonl")
    tracker = read_jsonl(ARTIFACTS / "tracker.jsonl")
    detector_failures = read_jsonl(SHARED_ARTIFACTS / "detector_failures.jsonl")
    reports = [evaluate(attempt) for attempt in (1, 2, 3)]
    status = "PASS"
    faults = []
    for path in [SHARED_ARTIFACTS / "clip.mp4", *review_media]:
        faults.extend(media_faults(path, video=True))
    for path in audio_media:
        faults.extend(media_faults(path, video=False))
    if faults:
        status = "FAIL"
    if detector_failures:
        status = "FAIL"
        faults.append(f"{len(detector_failures)} detector failures")
    if len(detections) != 150:
        status = "FAIL"
        faults.append(f"detection count {len(detections)} != 150")
    fast_scores = read_jsonl(OUTPUT_ARTIFACTS / "fast_scoreboard.jsonl")
    expected_tracker_rows = len(detections) + len(fast_scores)
    if len(tracker) != expected_tracker_rows:
        status = "FAIL"
        faults.append(
            f"tracker count {len(tracker)} != combined observer count "
            f"{expected_tracker_rows}"
        )
    if len(fast_scores) != 8:
        status = "FAIL"
        faults.append(f"fast scoreboard event count {len(fast_scores)} != 8")
    merged_stt = read_jsonl(SHARED_ARTIFACTS / "stt_merged.jsonl")
    if any(
        "transcribe only audible speech" in row.get("text", "").lower()
        for row in merged_stt
    ):
        status = "FAIL"
        faults.append("STT prompt echo survived merge")
    render_manifest = json.loads((ARTIFACTS / "render_manifest.json").read_text())
    if (
        render_manifest.get("profile") != PROFILE
        or render_manifest.get("voices") != VOICES
    ):
        status = "FAIL"
        faults.append("render manifest profile/voice IDs do not match configuration")
    if any(report["status"] != "PASS" for report in reports):
        status = "FAIL"
        faults.append("at least one attempt failed: worst-of-three gate")
    result = {
        "version": CONFIG["version"],
        "profile": PROFILE,
        "fixed_delay_s": DELAY_S,
        "status": status,
        "policy": "worst-of-three; any missing/malformed artifact fails",
        "detector_failures": len(detector_failures),
        "attempts": reports,
        "faults": faults,
    }
    out = ARTIFACTS / "gate.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if status != "PASS":
        raise SystemExit("tennis gate failed")


if __name__ == "__main__":
    main()
