#!/usr/bin/env python3
"""Measure cadence from public official full-match television captions.

The script stores only aggregate timing/word metrics and a source fingerprint.
It does not retain or republish the copyrighted caption transcript or video.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "benchmarks" / "wta_tv_cadence.json"
ACCOUNT = "6041795521001"
PLAYER = "te01Hqw71"
MEDIA_ID = "6397903308112"
SOURCE_PAGE = (
    "https://www.wtatennis.com/videos/4515906/"
    "full-match-replay-maria-outmaneuvers-anisimova-for-2025-queens-club-title"
)
WINDOWS = (
    {"label": "early_match", "start_s": 600.0, "end_s": 900.0},
    {"label": "middle_match", "start_s": 1800.0, "end_s": 2100.0},
    {"label": "late_match", "start_s": 3000.0, "end_s": 3300.0},
)
TIMESTAMP = re.compile(
    r"(?:(?P<hours>\d{2}):)?(?P<minutes>\d{2}):"
    r"(?P<seconds>\d{2}\.\d{3})"
)


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"Accept-Encoding": "gzip", **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object from {url}")
    return value


def timestamp_seconds(value: str) -> float:
    match = TIMESTAMP.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid VTT timestamp: {value!r}")
    return (
        3600 * int(match.group("hours") or 0)
        + 60 * int(match.group("minutes"))
        + float(match.group("seconds"))
    )


def parse_vtt(value: str) -> list[dict]:
    cues = []
    for block in value.replace("\r", "").split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if " --> " in line),
            None,
        )
        if timing_index is None:
            continue
        start_raw, end_raw = lines[timing_index].split(" --> ", 1)
        end_raw = end_raw.split()[0]
        text = " ".join(lines[timing_index + 1 :])
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = " ".join(text.split())
        if text:
            cues.append(
                {
                    "start_s": timestamp_seconds(start_raw),
                    "end_s": timestamp_seconds(end_raw),
                    "text": text,
                }
            )
    if not cues:
        raise ValueError("caption track contains no cues")
    return cues


def merged_intervals(
    cues: list[dict],
    start: float,
    end: float,
    tolerance: float,
) -> list[list[float]]:
    intervals = sorted(
        (max(start, row["start_s"]), min(end, row["end_s"]))
        for row in cues
        if row["end_s"] > start and row["start_s"] < end
    )
    merged: list[list[float]] = []
    for left, right in intervals:
        if merged and left <= merged[-1][1] + tolerance:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return merged


def merged_turns(
    cues: list[dict],
    start: float,
    end: float,
    tolerance: float = 1.0,
) -> list[dict]:
    selected = sorted(
        (
            {
                "start_s": max(start, row["start_s"]),
                "end_s": min(end, row["end_s"]),
                "text": row["text"],
            }
            for row in cues
            if row["end_s"] > start and row["start_s"] < end
        ),
        key=lambda row: row["start_s"],
    )
    turns: list[dict] = []
    for row in selected:
        if turns and row["start_s"] <= turns[-1]["end_s"] + tolerance:
            turns[-1]["end_s"] = max(turns[-1]["end_s"], row["end_s"])
            turns[-1]["text"] += " " + row["text"]
        else:
            turns.append(dict(row))
    return turns


def window_metrics(cues: list[dict], window: dict) -> dict:
    start, end = float(window["start_s"]), float(window["end_s"])
    selected = [
        row
        for row in cues
        if row["end_s"] > start and row["start_s"] < end
    ]
    audible = merged_intervals(cues, start, end, tolerance=0.25)
    turns = merged_turns(cues, start, end)
    gaps = (
        [
            audible[0][0] - start,
            *[
                later[0] - earlier[1]
                for earlier, later in zip(audible, audible[1:])
            ],
            end - audible[-1][1],
        ]
        if audible
        else [end - start]
    )
    duration = end - start
    captioned = sum(right - left for left, right in audible)
    words = sum(
        len(re.findall(r"\b[\w']+\b", row["text"]))
        for row in selected
    )
    return {
        **window,
        "caption_cues": len(selected),
        "merged_speech_turns": len(turns),
        "turns_per_minute": round(len(turns) / (duration / 60), 3),
        "words": words,
        "words_per_minute": round(words / (duration / 60), 3),
        "captioned_s": round(captioned, 3),
        "caption_occupancy": round(captioned / duration, 4),
        "maximum_caption_silence_s": round(max(gaps), 3),
    }


def load_reference() -> tuple[dict, str, list[dict]]:
    config = get_json(
        f"https://players.brightcove.net/{ACCOUNT}/{PLAYER}_default/config.json"
    )
    policy_key = config["video_cloud"]["policy_key"]
    video = get_json(
        (
            "https://edge.api.brightcove.com/playback/v1/accounts/"
            f"{ACCOUNT}/videos/{MEDIA_ID}"
        ),
        {"Accept": f"application/json;pk={policy_key}"},
    )
    tracks = [
        track
        for track in video.get("text_tracks", [])
        if track.get("kind") == "captions"
        and track.get("srclang") == "en"
        and track.get("src")
    ]
    if len(tracks) != 1:
        raise ValueError(f"expected one English caption track, found {len(tracks)}")
    with urllib.request.urlopen(tracks[0]["src"], timeout=30) as response:
        raw = response.read()
    caption_text = raw.decode("utf-8", "replace")
    return video, caption_text, parse_vtt(caption_text)


def main() -> None:
    video, caption_text, cues = load_reference()
    windows = [window_metrics(cues, window) for window in WINDOWS]
    result = {
        "source": {
            "publisher": "WTA",
            "page": SOURCE_PAGE,
            "media_id": MEDIA_ID,
            "name": video.get("name"),
            "duration_s": round(float(video.get("duration", 0)) / 1000, 3),
            "continuous_full_match": True,
            "english_captions": True,
            "caption_sha256": hashlib.sha256(
                caption_text.encode("utf-8")
            ).hexdigest(),
        },
        "method": {
            "window_selection": "three fixed five-minute windows at 10, 30, and 50 minutes",
            "occupancy": "union of caption cue intervals; 0.25s split tolerance",
            "speech_turn": "caption intervals merged across gaps <= 1.0s",
            "copyright": "aggregate metrics only; transcript and video are not retained",
        },
        "windows": windows,
        "aggregate": {
            "sampled_minutes": 15,
            "mean_turns_per_minute": round(
                sum(item["turns_per_minute"] for item in windows) / len(windows),
                3,
            ),
            "mean_words_per_minute": round(
                sum(item["words_per_minute"] for item in windows) / len(windows),
                3,
            ),
            "mean_caption_occupancy": round(
                sum(item["caption_occupancy"] for item in windows) / len(windows),
                4,
            ),
            "maximum_caption_silence_s": max(
                item["maximum_caption_silence_s"] for item in windows
            ),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
