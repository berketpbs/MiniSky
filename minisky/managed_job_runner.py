"""
Standalone managed-job runner entry point.

Run as: python -m minisky.managed_job_runner <job_id> [--check-interval-seconds N]

This is intentionally a separate OS process, for the same reason
autostop_runner.py is: ManagedJobController.start_monitoring() spawns an
in-process daemon thread, which is killed the instant the CLI command
that submitted the job returns and the Python interpreter exits - so
spot preemption recovery never actually survived past a single
invocation. cli.py's `minisky jobs launch` spawns *this* module as a
detached child process instead, which is what actually outlives the
submitting CLI command and can relaunch the job when its spot VM gets
preempted, however long from now that happens.
"""

import argparse
import logging
import sys

from .console_utils import ensure_utf8_console

ensure_utf8_console()

from .config import MiniSkyConfig
from .state import StateManager
from .executor import Executor
from .providers import get_provider
from .storage import StorageManager
from .managed_jobs import ManagedJob, ManagedJobController, ManagedJobStatus


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MiniSky managed-job runner (internal - normally spawned by `minisky jobs launch`)"
    )
    parser.add_argument("job_id", help="Managed job ID to run")
    parser.add_argument("--check-interval-seconds", type=int, default=30, help="Seconds between VM/cancellation checks")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [managed-job] %(message)s",
    )

    config = MiniSkyConfig()
    state = StateManager()

    data = state.get_managed_job_data(args.job_id)
    if not data:
        print(f"Managed job not found: {args.job_id}", flush=True)
        return 1

    job = ManagedJob.from_dict(data)
    if job.task is None:
        print(f"Managed job {args.job_id} has no stored task definition, cannot run", flush=True)
        return 1

    task = job.task

    try:
        provider = get_provider(task.provider)
    except Exception as e:
        print(f"Could not load provider '{task.provider}': {e}", flush=True)
        return 1

    controller = ManagedJobController(
        state_manager=state,
        provider=provider,
        storage_manager=StorageManager(),
        executor_factory=lambda vm_info: Executor(vm_info),
    )
    with controller._lock:
        controller._jobs[job.job_id] = job

    print(
        f"Managed job runner started for {args.job_id} "
        f"(checking every {args.check_interval_seconds}s)",
        flush=True,
    )

    final_status = controller.run_to_completion(
        job, task, check_interval=args.check_interval_seconds
    )

    print(f"Managed job runner exiting for {args.job_id}: {final_status.value}", flush=True)
    return 0 if final_status == ManagedJobStatus.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
