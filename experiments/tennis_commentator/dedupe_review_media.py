#!/usr/bin/env python3
"""Replace verified deployment copies with hard links to immutable artifacts."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARTIFACTS = BASE / "artifacts"
WEB = Path("/var/www/html/experiments/tennis_commentator")
PROFILES = ("10s", "6s")
LANGUAGES = {
    "english": "en",
    "french": "fr",
    "portuguese": "pt",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pair(source: Path, destination: Path) -> tuple[bool, int]:
    if not source.is_file() or not destination.is_file():
        raise SystemExit(f"missing dedupe pair: {source} -> {destination}")
    source_stat = source.stat()
    destination_stat = destination.stat()
    if (
        source_stat.st_dev == destination_stat.st_dev
        and source_stat.st_ino == destination_stat.st_ino
    ):
        return False, 0
    if source_stat.st_size != destination_stat.st_size:
        raise SystemExit(f"size mismatch: {source} -> {destination}")
    if sha256(source) != sha256(destination):
        raise SystemExit(f"hash mismatch: {source} -> {destination}")
    if source_stat.st_dev != destination_stat.st_dev:
        raise SystemExit(f"cannot hard-link across filesystems: {destination}")
    return True, source_stat.st_size


def link_verified(source: Path, destination: Path) -> tuple[bool, int]:
    should_link, size = verify_pair(source, destination)
    if not should_link:
        return False, 0
    temporary = destination.with_name(f".{destination.name}.dedupe.tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True, size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "versions",
        nargs="*",
        default=["v1", "v2", "v3"],
        choices=("v1", "v2", "v3"),
    )
    args = parser.parse_args()
    pairs = []
    for version in args.versions:
        for profile in PROFILES:
            root = WEB / f"{version}_{profile}"
            pairs.append(
                (ARTIFACTS / "v1" / "clip.mp4", root / "original.mp4")
            )
            pairs.extend(
                (
                    ARTIFACTS / version / profile
                    / f"review_{artifact_language}.mp4",
                    root / f"ai_{deployed_language}.mp4",
                )
                for deployed_language, artifact_language in LANGUAGES.items()
            )

    # Validate every target before replacing any of them.
    for source, destination in pairs:
        verify_pair(source, destination)

    changed = 0
    logical_bytes = 0
    for source, destination in pairs:
        linked, size = link_verified(source, destination)
        changed += int(linked)
        logical_bytes += size
    print(
        f"deduplicated {changed} verified deployment files "
        f"({logical_bytes / 1024 / 1024:.1f} MiB logical copies)"
    )


if __name__ == "__main__":
    main()
