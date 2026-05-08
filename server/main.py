"""Production server entry point.

Usage:
    python3 -m server.main --config matches.yaml [--dry-run]
"""

import argparse
import os
import signal
import sys
import time

# Load .env before anything else
def _load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

from server.config import load_config, validate_config
from server.orchestrator import Orchestrator
from server.status_api import start_status_server


def main():
    parser = argparse.ArgumentParser(
        description="Production match server: 1 STT → N languages per match"
    )
    parser.add_argument("--config", required=True, help="Path to matches.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate config and exit")
    args = parser.parse_args()

    cfg = load_config(args.config)

    try:
        validate_config(cfg, dry_run=args.dry_run)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("Config valid. Matches:")
        for m in cfg.matches:
            print(f"  {m.match_id}: {len(m.languages)} languages "
                  f"({', '.join(m.languages)})")
        print("Dry run complete.")
        return

    orchestrator = Orchestrator(cfg)
    start_status_server(cfg.control_port, orchestrator, cfg)
    orchestrator.scheduler.start()

    print(f"\n{'=' * 70}")
    print(f"  PRODUCTION SERVER — {len(cfg.matches)} match(es)")
    for m in cfg.matches:
        mode_str = f"[{m.mode}]"
        if not m.enabled:
            mode_str += " disabled"
        elif m.auto_manage:
            mode_str += " auto"
        langs = ", ".join(m.languages)
        print(f"  {m.match_id}: {mode_str} {langs}")
    print(f"  Control:  http://localhost:{cfg.control_port}/")
    print(f"  Status:   http://localhost:{cfg.control_port}/status.html")
    print(f"  Viewer:   http://localhost:{cfg.control_port}/viewer_live.html")
    print(f"{'=' * 70}\n")

    # Demo matches stay idle until started via API
    # Live auto_manage matches are handled by the scheduler

    # Wait for shutdown signal
    shutdown = False
    def handle_signal(signum, frame):
        nonlocal shutdown
        if not shutdown:
            shutdown = True
            print("\n\n  Shutting down...")
            orchestrator.scheduler.stop()
            orchestrator.stop_all()
            print("  Done.")
            sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_signal(None, None)


if __name__ == "__main__":
    main()
