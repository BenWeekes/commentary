#!/usr/bin/env python3
"""Post the passing tennis review URL through an explicitly configured webhook."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import time

import requests

from feedback_server import ROUNDS
from tennis_common import CONFIG, OUTPUT_ARTIFACTS, PROFILES, load_env


def page_has_title(body: bytes, title: str) -> bool:
    try:
        return title in body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False


def main() -> None:
    if len(sys.argv) != len(PROFILES) + 1 or any(
        not value.startswith("https://") for value in sys.argv[1:]
    ):
        names = " ".join(f"https://{profile}-review-url/" for profile in PROFILES)
        raise SystemExit(f"usage: announce_slack.py {names}")
    version = CONFIG["version"]
    load_env()
    title = f"AI Tennis commentator — {version} ready for review"
    rounds = json.loads(ROUNDS.read_text())
    record = (rounds.get("rounds") or {}).get(version) or {}
    if rounds.get("current") != version or record.get("status") != "open":
        raise SystemExit(f"Slack announcement requires open review round {version}")
    for profile in PROFILES:
        gate = OUTPUT_ARTIFACTS / profile / "gate.json"
        if not gate.exists() or json.loads(gate.read_text()).get("status") != "PASS":
            raise SystemExit(f"Slack announcement requires a passing {profile} gate")
    expected_suffixes = tuple(f"/{version}_{profile}/" for profile in PROFILES)
    for url, suffix in zip(sys.argv[1:], expected_suffixes):
        if not url.endswith(suffix):
            raise SystemExit(f"review URL must end with {suffix}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        if not page_has_title(response.content, title):
            raise SystemExit(f"review page marker missing from {url}")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("SLACK_WEBHOOK_URL is not configured; announcement stopped")
    message = {
        "text": (
            f"{title}\n"
            "Glinka vs Mayo, exact five-minute "
            "clip from 02:00:15. Same six review columns and review workflow as football.\n"
            + "\n".join(
                f"{profile.removesuffix('s')}-second profile: {url}"
                for profile, url in zip(PROFILES, sys.argv[1:])
            )
        )
    }
    response = requests.post(webhook, json=message, timeout=30)
    response.raise_for_status()
    if response.text.strip().lower() != "ok":
        raise SystemExit(f"Slack returned unexpected response: {response.text[:100]!r}")
    receipt = {
        "delivered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status_code": response.status_code,
        "response": response.text.strip(),
        "webhook_sha256_prefix": hashlib.sha256(webhook.encode()).hexdigest()[:12],
        "urls": sys.argv[1:],
    }
    OUTPUT_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ARTIFACTS / "slack_delivery.json").write_text(
        json.dumps(receipt, indent=2)
    )
    print("Slack announcement accepted")


if __name__ == "__main__":
    main()
