#!/usr/bin/env python3
"""Open the configured review round only after its predecessor is disposed."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from feedback_server import ROUNDS
from tennis_common import CONFIG, OUTPUT_ARTIFACTS, PROFILES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", required=True)
    args = parser.parse_args()
    version = CONFIG["version"]
    title = f"AI Tennis commentator — {version} ready for review"
    state = json.loads(ROUNDS.read_text())
    previous = (state.get("rounds") or {}).get(args.previous) or {}
    if previous.get("status") != "closed":
        raise SystemExit(f"previous round {args.previous} is not closed")
    for profile in PROFILES:
        gate_path = OUTPUT_ARTIFACTS / profile / "gate.json"
        if (
            not gate_path.exists()
            or json.loads(gate_path.read_text()).get("status") != "PASS"
        ):
            raise SystemExit(f"round {version} requires a passing {profile} gate")
        page = (
            Path("/var/www/html/experiments/tennis_commentator")
            / f"{version}_{profile}"
            / "index.html"
        )
        if not page.exists() or title not in page.read_text():
            raise SystemExit(f"round {version} requires a built {profile} review page")
    record = (state.get("rounds") or {}).get(version)
    if record:
        if record.get("status") != "open" or state.get("current") != version:
            raise SystemExit(f"round {version} exists in an incompatible state")
        print(f"round {version} already open")
        return
    state["current"] = version
    state.setdefault("rounds", {})[version] = {
        "status": "open",
        "opened": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clip": CONFIG["clip"]["id"],
        "profiles": list(PROFILES),
    }
    temporary = ROUNDS.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    os.replace(temporary, ROUNDS)
    print(f"opened review round {version}")


if __name__ == "__main__":
    main()
