"""
Standalone autostop watcher entry point.

Run as: python -m minisky.autostop_runner <vm_id> --timeout-minutes N

This is intentionally a separate OS process. `minisky launch` used to
start autostop monitoring as an in-process daemon thread
(threading.Thread(daemon=True)), which is killed the instant the
launching CLI command returns and the Python interpreter exits - so it
never actually survived long enough to watch anything. cli.py's
_spawn_autostop_watcher() spawns *this* module as a detached child
process instead, which is what actually outlives `minisky launch` and
enforces the configured autostop timeout.
"""

import argparse
import logging
import sys

from .console_utils import ensure_utf8_console

ensure_utf8_console()

from .autostop_agent import AutostopAgent, AutostopConfig
from .config import MiniSkyConfig
from .state import StateManager


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MiniSky autostop watcher (internal - normally spawned by `minisky launch`)"
    )
    parser.add_argument("vm_id", help="VM ID to monitor")
    parser.add_argument("--timeout-minutes", type=int, required=True, help="Idle timeout in minutes")
    parser.add_argument("--check-interval-seconds", type=int, default=60, help="Seconds between checks")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [autostop] %(message)s",
    )

    agent = AutostopAgent(
        config=MiniSkyConfig(),
        state=StateManager(),
        autostop_config=AutostopConfig(
            idle_timeout_minutes=args.timeout_minutes,
            check_interval_seconds=args.check_interval_seconds,
        ),
    )

    print(
        f"Autostop watcher started for {args.vm_id} "
        f"(timeout: {args.timeout_minutes}m, checking every {args.check_interval_seconds}s)",
        flush=True,
    )

    try:
        agent.watch_until_stopped(args.vm_id, timeout_minutes=args.timeout_minutes)
    except KeyboardInterrupt:
        pass

    print(f"Autostop watcher exiting for {args.vm_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
