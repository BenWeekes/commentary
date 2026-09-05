#!/usr/bin/env python3
"""Close an empty predecessor through the live review service, fail-closed."""
from __future__ import annotations

import argparse
import json
import subprocess
import time

import requests

from feedback_server import FEEDBACK, ROUNDS, digest, trigger_pin
from tennis_common import CONFIG


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", required=True)
    args = parser.parse_args()
    version = CONFIG["version"]
    if args.previous == version:
        raise SystemExit("previous round cannot equal configured version")

    state = json.loads(ROUNDS.read_text())
    previous = (state.get("rounds") or {}).get(args.previous) or {}
    reviewed = digest(args.previous)
    if reviewed["total_items"] != 0:
        raise SystemExit(
            f"{args.previous} has {reviewed['total_items']} review points; "
            "supersession stopped"
        )

    trigger_path = FEEDBACK / f"trigger_{args.previous}.json"
    if previous.get("status") == "open":
        if state.get("current") != args.previous:
            raise SystemExit("only the current open round can be superseded")
        pin = trigger_pin()
        if not pin:
            raise SystemExit("review trigger PIN is unavailable")
        response = requests.post(
            "http://127.0.0.1:8092/tennis_trigger",
            json={
                "version": args.previous,
                "pin": pin,
                "triggered_by": f"superseded_by_{version}_at_user_direction",
            },
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SystemExit("review service returned malformed JSON") from exc
        if response.status_code != 200:
            raise SystemExit(
                f"review service refused supersession: "
                f"{payload.get('error', response.status_code)}"
            )
        if payload.get("items") != 0:
            raise SystemExit(
                "feedback arrived during supersession; next round remains stopped"
            )
    elif previous.get("status") != "closed":
        raise SystemExit(f"previous round {args.previous} is not open or closed")

    if not trigger_path.exists():
        raise SystemExit(f"review service did not create {trigger_path}")
    work = json.loads(trigger_path.read_text())
    if work.get("total_items") != 0 or work.get("all_items") != []:
        raise SystemExit("closed-round work order is not empty; stopped")

    disposition_path = FEEDBACK / f"dispositions_{args.previous}.json"
    if not disposition_path.exists():
        disposition_path.write_text(
            json.dumps(
                {
                    "version": args.previous,
                    "disposed_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                    "note": (
                        f"No submitted feedback; superseded by {version} at "
                        "explicit user direction. Existing review pages remain "
                        "available for side-by-side comparison."
                    ),
                    "items": [],
                },
                indent=2,
            )
        )
    subprocess.run(
        [
            "/home/ubuntu/commentary/.venv/bin/python",
            "check_feedback.py",
            "--round",
            args.previous,
        ],
        check=True,
    )
    print(f"{args.previous}: empty review round safely superseded")


if __name__ == "__main__":
    main()
