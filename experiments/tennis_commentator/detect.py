#!/usr/bin/env python3
"""Run the conservative tennis observer over chronological frame bursts."""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from tennis_common import (
    CLIP,
    CONFIG,
    SHARED_ARTIFACTS as ARTIFACTS,
    assert_football_idle,
    load_env,
    require_env,
)

FRAMES = ARTIFACTS / "frames_1fps"
OUT = ARTIFACTS / "detections.jsonl"
FAILURES = ARTIFACTS / "detector_failures.jsonl"
PROMPT = (Path(__file__).parent / "prompts" / "detector_v1.txt").read_text()
INTERVAL = float(CONFIG["timing"]["analysis_interval_seconds"])


def extract_json(text: str) -> dict:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("detector response must be an object")
    return value


def validate(value: dict) -> dict:
    phases = {"serve_setup", "rally", "point_ended", "between_points",
              "changeover", "replay", "unknown"}
    if value.get("phase") not in phases:
        raise ValueError("invalid phase")
    live_conf = value.get("live_play_confidence")
    if not isinstance(live_conf, (int, float)) or not 0 <= live_conf <= 1:
        raise ValueError("invalid live_play_confidence")
    board = value.get("scoreboard")
    if not isinstance(board, dict) or not isinstance(board.get("visible"), bool):
        raise ValueError("invalid scoreboard")
    board_conf = board.get("confidence")
    if not isinstance(board_conf, (int, float)) or not 0 <= board_conf <= 1:
        raise ValueError("invalid scoreboard confidence")
    if board.get("server") not in {"far", "near", "unknown"}:
        raise ValueError("invalid server")
    if not board["visible"]:
        for side in ("far", "near"):
            for part in ("sets", "games", "points"):
                board[f"{side}_{part}"] = None
        board["server"] = "unknown"
        return value
    for side in ("far", "near"):
        for part in ("sets", "games"):
            key = f"{side}_{part}"
            item = board.get(key)
            if isinstance(item, str) and item.strip().isdigit():
                item = int(item.strip())
                board[key] = item
            elif isinstance(item, str) and item.strip().lower() in {"", "unknown", "unreadable"}:
                item = None
                board[key] = None
            if item is not None and (type(item) is not int or item < 0):
                raise ValueError(f"invalid {key}: {item!r}")
        point = board.get(f"{side}_points")
        if isinstance(point, str):
            point = point.strip().upper()
            if point in {"A", "ADV", "ADVANTAGE"}:
                point = "AD"
            elif point in {"", "UNKNOWN", "UNREADABLE"}:
                point = None
        elif type(point) is int and point >= 0:
            point = str(point)
        if (
            point is not None
            and (
                not isinstance(point, str)
                or (point not in {"0", "15", "30", "40", "AD"} and not point.isdigit())
            )
        ):
            raise ValueError(f"invalid {side}_points: {point!r}")
        if point is not None:
            board[f"{side}_points"] = point
        else:
            board[f"{side}_points"] = None
    point = value.get("point")
    if not isinstance(point, dict) or not isinstance(point.get("ended_in_burst"), bool):
        raise ValueError("invalid point")
    if point.get("winner") not in {"far", "near", "unknown"}:
        raise ValueError("invalid point winner")
    point_conf = point.get("confidence")
    if not isinstance(point_conf, (int, float)) or not 0 <= point_conf <= 1:
        raise ValueError("invalid point confidence")
    observation = value.get("observation")
    if not isinstance(observation, str) or len(observation) > 300:
        raise ValueError("invalid observation")
    return value


def as_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return "data:image/jpeg;base64," + encoded


def ensure_frames() -> None:
    if len(list(FRAMES.glob("f_*.jpg"))) >= 299:
        return
    assert_football_idle()
    FRAMES.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", str(CLIP),
            "-vf", "fps=1,scale=960:540", "-q:v", "4", "-start_number", "0",
            "-y", str(FRAMES / "f_%04d.jpg"),
        ],
        check=True,
    )


def analyze(client: OpenAI, center: float) -> dict:
    indexes = sorted({max(0, min(299, int(center) + offset)) for offset in (-1, 0, 1)})
    paths = [FRAMES / f"f_{index:04d}.jpg" for index in indexes]
    if not all(path.exists() for path in paths):
        raise FileNotFoundError(f"missing frame burst at {center:.1f}s")
    content = [{"type": "input_text", "text": f"Frame burst centered at clip t={center:.1f}s."}]
    content.extend({"type": "input_image", "image_url": as_data_url(path)} for path in paths)
    assert_football_idle()
    started = time.monotonic()
    response = client.responses.create(
        model=CONFIG["models"]["vision"],
        instructions=PROMPT,
        input=[{"role": "user", "content": content}],
        max_output_tokens=350,
    )
    latency = time.monotonic() - started
    value = validate(extract_json(response.output_text or ""))
    return {
        "video_time_s": round(center, 3),
        "frame_indexes": indexes,
        "latency_s": round(latency, 3),
        "detection": value,
    }


def main() -> None:
    assert_football_idle()
    load_env()
    require_env(["OPENAI_API_KEY"])
    if not CLIP.exists():
        raise SystemExit(f"missing clip: {CLIP}; run prepare_clip.sh first")
    ensure_frames()
    centers = [round(value, 3) for value in frange(1.0, 300.0, INTERVAL)]
    rows = []
    if OUT.exists():
        for raw in OUT.read_text().splitlines():
            if raw.strip():
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    rows = []
                    break
    completed = {float(row["video_time_s"]) for row in rows}
    centers = [center for center in centers if center not in completed]
    if not centers:
        FAILURES.write_text("")
        print(f"{OUT} already contains every detection")
        return
    client = OpenAI()
    failures = []
    total = len(centers)
    finished = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(analyze, client, center): center for center in centers}
        for done in as_completed(jobs):
            center = jobs[done]
            finished += 1
            try:
                row = done.result()
                rows.append(row)
                print(f"[vision retry {finished:3d}/{total}] {center:6.1f}s "
                      f"{row['detection']['phase']}")
            except Exception as exc:
                failure = {
                    "video_time_s": center,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                failures.append(failure)
                print(f"[vision failure] {center:.1f}s {failure['error_type']}: {failure['error']}")
    rows.sort(key=lambda item: item["video_time_s"])
    failures.sort(key=lambda item: item["video_time_s"])
    OUT.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    FAILURES.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in failures))
    if failures:
        raise SystemExit(f"{len(failures)} detector calls failed; fail-closed artifacts retained")
    print(f"wrote {OUT} ({len(rows)} detections)")


def frange(start: float, stop: float, step: float):
    value = start
    while value < stop:
        yield value
        value += step


if __name__ == "__main__":
    main()
