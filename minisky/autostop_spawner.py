"""
Shared logic for spawning a detached autostop watcher process.

An in-process threading.Thread (even daemon=True) is killed the instant
the process that started it exits, so it can never really watch
anything past a single invocation - this spawns minisky/autostop_runner.py
as its own OS process, detached from the caller (new session on POSIX,
DETACHED_PROCESS on Windows), so it keeps running after the caller
returns/exits.

Used by both cli.py (`minisky launch`) and api/core.py's
ClusterController (`minisky serve` / dashboard-launched clusters) -
factored out here so a VM launched through either path gets the same
autostop protection instead of only the CLI path having it.
"""

import subprocess
import sys
from pathlib import Path

from .config import MiniSkyConfig


def spawn_autostop_watcher(vm_id: str, timeout_minutes: int, config: MiniSkyConfig) -> Path:
    """
    Launch a detached background OS process that actually enforces
    autostop for vm_id.

    Returns the path to the watcher's log file.
    """
    log_path = config.log_dir / f"autostop-{vm_id}.log"
    cmd = [
        sys.executable, "-m", "minisky.autostop_runner",
        vm_id, "--timeout-minutes", str(timeout_minutes),
    ]

    popen_kwargs = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    log_file = open(log_path, "a")
    try:
        subprocess.Popen(cmd, stdout=log_file, stderr=log_file, **popen_kwargs)
    finally:
        log_file.close()

    return log_path
