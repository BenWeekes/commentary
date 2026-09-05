#!/usr/bin/env python3
"""Validate and fingerprint immutable clip/STT/vision inputs reused across rounds."""
from __future__ import annotations

import hashlib
import json
import subprocess

from tennis_common import (
    CONFIG,
    OUTPUT_ARTIFACTS,
    SHARED_ARTIFACTS,
    PipelineError,
    assert_football_idle,
    read_jsonl,
)


def digest(path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    assert_football_idle()
    names = (
        "clip.mp4",
        "stt.jsonl",
        "stt_whisper.jsonl",
        "stt_merged.jsonl",
        "stt_rejected.jsonl",
        "detections.jsonl",
        "detector_failures.jsonl",
    )
    paths = [SHARED_ARTIFACTS / name for name in names]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise PipelineError("missing immutable input artifacts: " + ", ".join(missing))
    duration = float(
        subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(SHARED_ARTIFACTS / "clip.mp4"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    detections = read_jsonl(SHARED_ARTIFACTS / "detections.jsonl")
    failures = read_jsonl(SHARED_ARTIFACTS / "detector_failures.jsonl")
    if abs(duration - 300.0) > 0.01:
        raise PipelineError(f"input clip duration {duration:.6f}s != 300s")
    if len(detections) != 150 or failures:
        raise PipelineError(
            f"invalid vision input: detections={len(detections)}, failures={len(failures)}"
        )
    manifest = {
        "version": CONFIG["version"],
        "input_artifacts_version": CONFIG.get("input_artifacts_version"),
        "duration_s": duration,
        "detections": len(detections),
        "detector_failures": len(failures),
        "sha256": {path.name: digest(path) for path in paths},
    }
    OUTPUT_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_ARTIFACTS / "input_manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"validated immutable inputs -> {out}")


if __name__ == "__main__":
    main()
