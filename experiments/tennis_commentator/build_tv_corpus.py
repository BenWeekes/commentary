#!/usr/bin/env python3
"""Build a retention-safe corpus from official full-match tennis broadcasts.

Source captions or transient STT text are used only in memory. Persisted
artifacts contain cadence measurements, semantic labels, and short paraphrases;
they never contain the broadcast transcript or media.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import statistics
import subprocess
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

import requests
from openai import OpenAI

from benchmark_tv_commentary import merged_turns, parse_vtt, window_metrics
from tennis_common import (
    BASE,
    CONFIG,
    PipelineError,
    assert_football_idle,
    load_env,
    require_env,
)

SOURCES_PATH = BASE / "tv_corpus_sources.json"
OUTPUT_DIR = BASE / "benchmarks" / "tv_corpus"
SUMMARY_PATH = BASE / "benchmarks" / "tv_corpus_summary.json"
PARAPHRASE_PATH = BASE / "benchmarks" / "tv_corpus_paraphrases.jsonl"

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
FUNCTIONS = (
    "calls_live_action",
    "explains_previous_point",
    "states_score_or_server",
    "sets_up_next_point",
    "adds_player_context",
    "frames_match_stakes",
    "explains_tactics_or_technique",
    "discusses_conditions_or_venue",
    "banter_or_transition",
    "non_commentator_audio",
)
PARAPHRASE_FALLBACKS = {
    "calls_live_action": "Calls the live action.",
    "explains_previous_point": "Explains the previous point.",
    "states_score_or_server": "States the score or server.",
    "sets_up_next_point": "Sets up the next point.",
    "adds_player_context": "Adds relevant player context.",
    "frames_match_stakes": "Frames the match situation and stakes.",
    "explains_tactics_or_technique": "Explains a tactical or technical detail.",
    "discusses_conditions_or_venue": "Discusses the conditions or venue.",
    "banter_or_transition": "Provides a brief transition between points.",
    "non_commentator_audio": None,
}
NON_COMMENTARY = {"court_official_or_player", "uncertain"}
WORD_RE = re.compile(r"\b[\w']+\b", re.UNICODE)
SYSTEM = """Classify timestamped speech turns from a professional tennis
television broadcast. Return exactly one result for each indexed input turn.

Primary categories:
- score_or_server
- point_reaction_or_outcome
- tactics_or_pattern
- technique_or_shot
- player_background
- match_narrative_or_stakes
- conditions_or_venue
- banter_or_other
- court_official_or_player

Commentary functions:
- calls_live_action
- explains_previous_point
- states_score_or_server
- sets_up_next_point
- adds_player_context
- frames_match_stakes
- explains_tactics_or_technique
- discusses_conditions_or_venue
- banter_or_transition
- non_commentator_audio

Use court_official_or_player and non_commentator_audio for umpire, line judge,
or player speech rather than commentary. For every commentator turn, write a
plain factual paraphrase of at most 12 words describing what the commentator
is doing or saying. Do not quote, imitate, or preserve distinctive wording from
the source. For non-commentator audio, set paraphrase to null.

Return exactly a JSON array:
{"index": integer, "category": exact category, "function": exact function,
 "paraphrase": string or null}.
Do not include reasons or source text."""


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "gzip", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PipelineError(f"expected JSON object from {url}")
    return value


def load_sources() -> dict:
    value = json.loads(SOURCES_PATH.read_text())
    if value.get("schema_version") != 1:
        raise PipelineError("unsupported TV corpus source schema")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise PipelineError("TV corpus source list is empty")
    ids = [source.get("id") for source in sources]
    if any(not isinstance(item, str) or not item for item in ids):
        raise PipelineError("every TV corpus source needs an id")
    if len(ids) != len(set(ids)):
        raise PipelineError("TV corpus source ids must be unique")
    for source in sources:
        if not str(source.get("page", "")).startswith(
            "https://www.wtatennis.com/videos/"
        ):
            raise PipelineError(f"{source['id']}: source page is not official WTA")
        windows = source.get("windows")
        if source.get("benchmark_role") not in {
            "commentary_reference",
            "world_feed_control",
        }:
            raise PipelineError(f"{source['id']}: invalid benchmark role")
        if not isinstance(windows, list) or len(windows) != 3:
            raise PipelineError(f"{source['id']}: expected three windows")
        for window in windows:
            start = window.get("start_s")
            end = window.get("end_s")
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or end - start != 300
            ):
                raise PipelineError(
                    f"{source['id']}: every benchmark window must be 300 seconds"
                )
    return value


def playback_video(catalog: dict, media_id: str) -> dict:
    account = catalog["brightcove_account"]
    player = catalog["brightcove_player"]
    config = get_json(
        f"https://players.brightcove.net/{account}/{player}_default/config.json"
    )
    policy_key = config.get("video_cloud", {}).get("policy_key")
    if not policy_key:
        raise PipelineError("Brightcove player config is missing policy key")
    return get_json(
        (
            "https://edge.api.brightcove.com/playback/v1/accounts/"
            f"{account}/videos/{media_id}"
        ),
        {"Accept": f"application/json;pk={policy_key}"},
    )


def official_caption_cues(video: dict) -> tuple[list[dict], str]:
    tracks = [
        track
        for track in video.get("text_tracks", [])
        if track.get("kind") == "captions"
        and track.get("srclang") == "en"
        and track.get("src")
    ]
    if len(tracks) != 1:
        raise PipelineError(
            f"expected one official English caption track, found {len(tracks)}"
        )
    with urllib.request.urlopen(tracks[0]["src"], timeout=30) as response:
        raw = response.read()
    text = raw.decode("utf-8", "replace")
    return parse_vtt(text), hashlib.sha256(raw).hexdigest()


def https_mp4_source(video: dict) -> str:
    sources = [
        source
        for source in video.get("sources", [])
        if source.get("container") == "MP4"
        and str(source.get("src", "")).startswith("https://")
    ]
    if not sources:
        raise PipelineError("official playback metadata has no HTTPS MP4 source")
    sources.sort(
        key=lambda row: (
            int(row.get("avg_bitrate") or 10**12),
            int(row.get("height") or 10**12),
        )
    )
    return str(sources[0]["src"])


def extract_window(source_url: str, window: dict, output: Path) -> str:
    assert_football_idle()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-ss",
            str(window["start_s"]),
            "-i",
            source_url,
            "-t",
            "300",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output),
        ],
        check=True,
    )
    if not output.exists() or output.stat().st_size < 16000 * 2 * 290:
        raise PipelineError(f"transient audio extract is incomplete: {output}")
    return hashlib.sha256(output.read_bytes()).hexdigest()


def deepgram_cues(audio: Path, window: dict) -> list[dict]:
    assert_football_idle()
    response = requests.post(
        "https://api.deepgram.com/v1/listen",
        params={
            "model": CONFIG["models"]["stt"],
            "language": "en",
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
            "diarize": "true",
        },
        headers={
            "Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}",
            "Content-Type": "audio/wav",
        },
        data=audio.read_bytes(),
        timeout=360,
    )
    response.raise_for_status()
    utterances = response.json().get("results", {}).get("utterances")
    if not isinstance(utterances, list):
        raise PipelineError("Deepgram response is missing results.utterances[]")
    offset = float(window["start_s"])
    rows = []
    for item in utterances:
        text = str(item.get("transcript") or "").strip()
        confidence = item.get("confidence")
        if not text or not isinstance(confidence, (int, float)):
            continue
        if float(confidence) < 0.6:
            continue
        rows.append(
            {
                "start_s": offset + float(item["start"]),
                "end_s": offset + float(item["end"]),
                "text": text,
            }
        )
    if not rows:
        print(
            "no usable speech in transient window "
            f"{window['label']} ({window['start_s']:.0f}-{window['end_s']:.0f}s)"
        )
    return rows


def words(value: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(value)]


def has_four_word_overlap(source: str, paraphrase: str) -> bool:
    source_words = words(source)
    paraphrase_words = words(paraphrase)
    source_fours = {
        tuple(source_words[index : index + 4])
        for index in range(max(0, len(source_words) - 3))
    }
    return any(
        tuple(paraphrase_words[index : index + 4]) in source_fours
        for index in range(max(0, len(paraphrase_words) - 3))
    )


def parse_classification(value: str, turns: list[dict]) -> list[dict]:
    clean = value.strip()
    if clean.startswith("```"):
        clean = clean.removeprefix("```json").removeprefix("```")
        clean = clean.removesuffix("```").strip()
    rows = json.loads(clean)
    if not isinstance(rows, list) or len(rows) != len(turns):
        raise PipelineError("TV classifier returned wrong row count")
    by_index = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PipelineError("TV classifier row is not an object")
        index = row.get("index")
        category = row.get("category")
        function = row.get("function")
        paraphrase = row.get("paraphrase")
        if not isinstance(index, int) or not 0 <= index < len(turns):
            raise PipelineError("TV classifier index is invalid")
        if index in by_index:
            raise PipelineError("TV classifier indexes are duplicated")
        if category not in CATEGORIES or function not in FUNCTIONS:
            raise PipelineError("TV classifier label is invalid")
        if category == "court_official_or_player":
            paraphrase = None
            paraphrase_guard_status = "withheld_noncommentator"
        else:
            unsafe_reason = None
            if not isinstance(paraphrase, str) or not paraphrase.strip():
                unsafe_reason = "missing"
            elif len(words(paraphrase)) > 12:
                unsafe_reason = "too_long"
            elif has_four_word_overlap(turns[index]["text"], paraphrase):
                unsafe_reason = "source_overlap"
            if unsafe_reason:
                paraphrase = PARAPHRASE_FALLBACKS[function]
                paraphrase_guard_status = f"safe_fallback_{unsafe_reason}"
            else:
                paraphrase_guard_status = "generated_safe"
        by_index[index] = {
            "index": index,
            "category": category,
            "function": function,
            "paraphrase": paraphrase.strip() if isinstance(paraphrase, str) else None,
            "paraphrase_guard_status": paraphrase_guard_status,
        }
    if sorted(by_index) != list(range(len(turns))):
        raise PipelineError("TV classifier indexes are missing")
    return [by_index[index] for index in range(len(turns))]


def classify_turns(turns: list[dict]) -> list[dict]:
    input_rows = [
        {
            "index": index,
            "start_s": round(float(turn["start_s"]), 3),
            "text": turn["text"],
        }
        for index, turn in enumerate(turns)
    ]
    client = OpenAI()
    attempts = []
    for attempt_number in range(1, 4):
        last_error: Exception | None = None
        for schema_try in range(1, 4):
            assert_football_idle()
            response = client.responses.create(
                model=CONFIG["models"]["commentary"],
                instructions=SYSTEM,
                input=json.dumps(input_rows, ensure_ascii=False),
                max_output_tokens=max(4000, len(turns) * 90),
            )
            try:
                parsed = parse_classification(
                    response.output_text or "",
                    turns,
                )
            except (PipelineError, json.JSONDecodeError) as exc:
                last_error = exc
                print(
                    "classifier schema retry "
                    f"{schema_try}/3 for consensus attempt {attempt_number}: {exc}"
                )
                continue
            attempts.append(parsed)
            break
        else:
            raise PipelineError(
                "TV classifier failed its guarded schema after three retries"
            ) from last_error
    result = []
    for index, turn in enumerate(turns):
        category_votes = Counter(
            attempt[index]["category"] for attempt in attempts
        )
        function_votes = Counter(
            attempt[index]["function"] for attempt in attempts
        )
        category, category_agreement = category_votes.most_common(1)[0]
        function, function_agreement = function_votes.most_common(1)[0]
        consensus_category = (
            category if category_agreement >= 2 else "uncertain"
        )
        consensus_function = (
            function if function_agreement >= 2 else "uncertain"
        )
        if consensus_category == "court_official_or_player":
            consensus_function = "non_commentator_audio"
        candidates = [
            attempt[index]
            for attempt in attempts
            if attempt[index]["category"] == category
            and attempt[index]["paraphrase"] is not None
        ]
        selected_paraphrase = candidates[0] if candidates else None
        result.append(
            {
                "index": index,
                "start_s": round(float(turn["start_s"]), 3),
                "end_s": round(float(turn["end_s"]), 3),
                "word_count": len(words(turn["text"])),
                "category": consensus_category,
                "category_agreement": category_agreement,
                "function": consensus_function,
                "function_agreement": function_agreement,
                "paraphrase": (
                    selected_paraphrase["paraphrase"]
                    if selected_paraphrase and consensus_category != "uncertain"
                    else None
                ),
                "paraphrase_guard_status": (
                    selected_paraphrase["paraphrase_guard_status"]
                    if selected_paraphrase and consensus_category != "uncertain"
                    else (
                        "withheld_noncommentator"
                        if consensus_category == "court_official_or_player"
                        else "withheld_uncertain"
                    )
                ),
            }
        )
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(float(ordered[index]), 3)


def semantic_metrics(rows: list[dict], sampled_minutes: float) -> dict:
    commentary = [
        row for row in rows if row["category"] not in NON_COMMENTARY
    ]
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in commentary:
        key = (str(row.get("source_id", "")), str(row.get("window", "")))
        grouped.setdefault(key, []).append(row)
    gaps = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda row: row["start_s"])
        gaps.extend(
            max(0.0, later["start_s"] - earlier["end_s"])
            for earlier, later in zip(ordered, ordered[1:])
        )
    word_counts = [float(row["word_count"]) for row in commentary]
    return {
        "all_speech_turns": len(rows),
        "commentator_turns": len(commentary),
        "commentator_turns_per_minute": round(
            len(commentary) / sampled_minutes, 3
        ),
        "median_words_per_turn": percentile(word_counts, 0.5),
        "p90_words_per_turn": percentile(word_counts, 0.9),
        "median_interturn_gap_s": percentile(gaps, 0.5),
        "p90_interturn_gap_s": percentile(gaps, 0.9),
        "maximum_interturn_gap_s": round(max(gaps), 3) if gaps else None,
        "category_counts": dict(
            sorted(Counter(row["category"] for row in rows).items())
        ),
        "function_counts": dict(
            sorted(Counter(row["function"] for row in rows).items())
        ),
    }


def process_source(catalog: dict, source: dict) -> dict:
    assert_football_idle()
    video = playback_video(catalog, source["media_id"])
    duration_s = float(video.get("duration", 0)) / 1000
    if duration_s < max(float(row["end_s"]) for row in source["windows"]):
        raise PipelineError(f"{source['id']}: source shorter than configured windows")

    transcript_source = source["transcript_source"]
    audio_hashes = []
    if transcript_source == "official_captions":
        cues, source_fingerprint = official_caption_cues(video)
    elif transcript_source == "deepgram_transient_audio":
        source_url = https_mp4_source(video)
        cues = []
        with tempfile.TemporaryDirectory(prefix="tennis_tv_corpus_") as folder:
            temp = Path(folder)
            for index, window in enumerate(source["windows"]):
                print(
                    f"extracting {source['id']} {window['label']} "
                    f"({window['start_s']:.0f}-{window['end_s']:.0f}s)"
                )
                audio = temp / f"window_{index}.wav"
                audio_hashes.append(extract_window(source_url, window, audio))
                cues.extend(deepgram_cues(audio, window))
                audio.unlink()
        source_fingerprint = hashlib.sha256(
            "".join(audio_hashes).encode("ascii")
        ).hexdigest()
    else:
        raise PipelineError(
            f"{source['id']}: unsupported transcript source {transcript_source!r}"
        )

    cadence = [
        window_metrics(cues, window)
        for window in source["windows"]
    ]
    turns = []
    for window in source["windows"]:
        for turn in merged_turns(
            cues,
            float(window["start_s"]),
            float(window["end_s"]),
        ):
            turns.append(
                {
                    "window": window["label"],
                    **turn,
                }
            )
    if not turns:
        raise PipelineError(f"{source['id']}: no speech turns in sampled windows")
    semantics = classify_turns(turns)
    for row, turn in zip(semantics, turns):
        row["window"] = turn["window"]

    return {
        "schema_version": 1,
        "source": {
            "id": source["id"],
            "publisher": catalog["publisher"],
            "page": source["page"],
            "media_id": source["media_id"],
            "name": video.get("name"),
            "duration_s": round(duration_s, 3),
            "surface": source["surface"],
            "match_type": source["match_type"],
            "stage": source["stage"],
            "benchmark_role": source["benchmark_role"],
            "transcript_source": transcript_source,
            "source_window_sha256": source_fingerprint,
        },
        "method": {
            "sampled_windows": source["windows"],
            "sampled_minutes": 15,
            "classifier_attempts": 3,
            "consensus": "at least two of three matching labels",
            "retention": (
                "source text and media are not retained; this file contains "
                "timings, counts, labels, and short guarded paraphrases only"
            ),
            "paraphrase_guard": (
                "maximum 12 words and no four-word source overlap"
            ),
        },
        "cadence_windows": cadence,
        "semantic_summary": semantic_metrics(semantics, 15),
        "paraphrase_rows": semantics,
    }


def write_aggregate(catalog: dict, results: list[dict]) -> None:
    all_rows = []
    commentary_rows = []
    commentary_cadence_windows = []
    for result in results:
        source_id = result["source"]["id"]
        benchmark_role = result["source"]["benchmark_role"]
        for row in result["paraphrase_rows"]:
            combined = {
                "source_id": source_id,
                "benchmark_role": benchmark_role,
                **row,
            }
            all_rows.append(combined)
            if benchmark_role == "commentary_reference":
                commentary_rows.append(combined)
        for row in result["cadence_windows"]:
            if benchmark_role == "commentary_reference":
                commentary_cadence_windows.append(
                    {"source_id": source_id, **row}
                )

    reference_results = [
        result
        for result in results
        if result["source"]["benchmark_role"] == "commentary_reference"
    ]
    control_results = [
        result
        for result in results
        if result["source"]["benchmark_role"] == "world_feed_control"
    ]
    if not reference_results:
        raise PipelineError("TV corpus has no commentary references")
    total_sampled_minutes = sum(
        float(result["method"]["sampled_minutes"]) for result in results
    )
    commentary_sampled_minutes = sum(
        float(result["method"]["sampled_minutes"])
        for result in reference_results
    )
    semantic = semantic_metrics(
        commentary_rows,
        commentary_sampled_minutes,
    )
    summary = {
        "schema_version": 1,
        "source_count": len(results),
        "commentary_reference_count": len(reference_results),
        "world_feed_control_count": len(control_results),
        "total_sampled_minutes": total_sampled_minutes,
        "commentary_reference_sampled_minutes": commentary_sampled_minutes,
        "sources": [result["source"] for result in results],
        "method": {
            "selection": (
                "official WTA full-match broadcasts spanning surfaces, "
                "singles/doubles, stages, and match phases"
            ),
            "three_windows_per_source": True,
            "retention": (
                "no source transcript, audio, or video retained; timestamped "
                "paraphrases are guarded against four-word source overlap"
            ),
        },
        "speech_cadence": {
            "mean_turns_per_minute": round(
                statistics.fmean(
                    float(row["turns_per_minute"])
                    for row in commentary_cadence_windows
                ),
                3,
            ),
            "mean_words_per_minute": round(
                statistics.fmean(
                    float(row["words_per_minute"])
                    for row in commentary_cadence_windows
                ),
                3,
            ),
            "mean_caption_occupancy": round(
                statistics.fmean(
                    float(row["caption_occupancy"])
                    for row in commentary_cadence_windows
                ),
                4,
            ),
            "maximum_speech_silence_s": max(
                float(row["maximum_caption_silence_s"])
                for row in commentary_cadence_windows
            ),
        },
        "commentary_semantics": semantic,
        "world_feed_controls": {
            result["source"]["id"]: result["semantic_summary"]
            for result in control_results
        },
        "per_source": {
            result["source"]["id"]: result["semantic_summary"]
            for result in results
        },
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )
    PARAPHRASE_PATH.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in all_rows
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild sources even when safe derived artifacts exist",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="build only this source id (repeatable)",
    )
    args = parser.parse_args()

    load_env()
    require_env(["OPENAI_API_KEY", "DEEPGRAM_API_KEY"])
    assert_football_idle()
    catalog = load_sources()
    selected = [
        source
        for source in catalog["sources"]
        if not args.source or source["id"] in args.source
    ]
    if args.source and len(selected) != len(set(args.source)):
        known = {source["id"] for source in catalog["sources"]}
        missing = sorted(set(args.source) - known)
        raise PipelineError("unknown TV corpus sources: " + ", ".join(missing))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for source in selected:
        output = OUTPUT_DIR / f"{source['id']}.json"
        if output.exists() and not args.refresh:
            result = json.loads(output.read_text())
            if (
                result.get("schema_version") != 1
                or result.get("source", {}).get("media_id") != source["media_id"]
            ):
                raise PipelineError(
                    f"{output} does not match source manifest; use --refresh"
                )
            role_migrated = (
                result.get("source", {}).get("benchmark_role")
                != source["benchmark_role"]
            )
            if role_migrated:
                result["source"]["benchmark_role"] = source["benchmark_role"]
            migrated_rows = False
            for row in result.get("paraphrase_rows", []):
                if row.get("category") == "court_official_or_player":
                    if (
                        row.get("paraphrase_guard_status")
                        != "withheld_noncommentator"
                    ):
                        row["paraphrase_guard_status"] = (
                            "withheld_noncommentator"
                        )
                        migrated_rows = True
                    continue
                if row.get("category") == "uncertain":
                    if (
                        row.get("paraphrase_guard_status")
                        != "withheld_uncertain"
                    ):
                        row["paraphrase_guard_status"] = "withheld_uncertain"
                        migrated_rows = True
                    continue
                if "paraphrase_guard_status" in row:
                    continue
                if row.get("paraphrase") is not None:
                    row["paraphrase_guard_status"] = (
                        "generated_safe_legacy_guarded"
                    )
                else:
                    row["paraphrase_guard_status"] = "withheld_uncertain"
                migrated_rows = True
            if migrated_rows or role_migrated:
                output.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False) + "\n"
                )
            print(f"reusing {output}")
        else:
            print(f"building {source['id']}")
            result = process_source(catalog, source)
            output.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n"
            )
            print(f"wrote {output}")
        results.append(result)

    if not args.source:
        write_aggregate(catalog, results)
        print(f"wrote {SUMMARY_PATH}")
        print(f"wrote {PARAPHRASE_PATH}")


if __name__ == "__main__":
    main()
