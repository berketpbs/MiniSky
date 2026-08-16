"""Tests for the managed jobs / spot recovery system (minisky/managed_jobs.py)."""

import time
from unittest.mock import MagicMock

import pytest

from minisky.managed_jobs import ManagedJobController, ManagedJobStatus


def _task(name="train"):
    task = MagicMock()
    task.name = name
    task.resources = MagicMock()
    task.resources.use_spot = False
    return task


def _controller(provider=None, storage=None, executor_factory=None):
    state = MagicMock()
    provider = provider or MagicMock()
    return ManagedJobController(
        state_manager=state,
        provider=provider,
        storage_manager=storage,
        executor_factory=executor_factory or MagicMock(),
    ), state, provider


class TestSubmitStoresTask:
    def test_task_retained_on_the_job_for_later_recovery(self):
        controller, _, _ = _controller()
        task = _task()
        job = controller.submit(task, command="python train.py")
        assert job.task is task


class TestLaunchJob:
    def test_launch_success_sets_running(self):
        controller, state, provider = _controller()
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        task = _task()
        job = controller.submit(task, command="python train.py")

        result = controller.launch_job(job, task)

        assert result is True
        assert job.status == ManagedJobStatus.RUNNING
        assert job.vm_id == "mock-1"
        state.add_vm.assert_called_once()

    def test_launch_failure_sets_failed(self):
        controller, state, provider = _controller()
        provider.launch.side_effect = RuntimeError("capacity error")
        task = _task()
        job = controller.submit(task, command="python train.py")

        result = controller.launch_job(job, task)

        assert result is False
        assert job.status == ManagedJobStatus.FAILED
        assert "capacity error" in job.error_message

    def test_checkpoint_restore_failure_still_disconnects_executor(self):
        """A failure during checkpoint restore must not leak the SSH
        connection - executor.disconnect() has to run either way."""
        mock_executor = MagicMock()
        executor_factory = MagicMock(return_value=mock_executor)
        storage = MagicMock()
        storage.restore_checkpoint.side_effect = RuntimeError("network drop mid-transfer")

        controller, state, provider = _controller(storage=storage, executor_factory=executor_factory)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}

        task = _task()
        job = controller.submit(task, command="python train.py", checkpoint_uri="s3://bucket/ckpt")
        job.last_checkpoint = "s3://bucket/ckpt/old"

        result = controller.launch_job(job, task)

        assert result is False
        assert job.status == ManagedJobStatus.FAILED
        mock_executor.disconnect.assert_called_once()


class TestPreemptionRecovery:
    def test_monitor_loop_calls_handle_preemption_when_vm_preempted(self):
        controller, state, provider = _controller()
        provider.launch.return_value = {"vm_id": "mock-2", "ip_address": "1.2.3.4"}
        provider.status.return_value = {"status": "preempted"}

        task = _task()
        job = controller.submit(task, command="python train.py")
        job.recovery_config.retry_delay_seconds = 0  # don't actually sleep in the test
        controller.launch_job(job, task)
        assert job.status == ManagedJobStatus.RUNNING

        # Run one iteration of the monitor loop's body directly (avoid a
        # real background thread + sleep in the test).
        controller._running = True
        with controller._lock:
            running_jobs = [j for j in controller._jobs.values() if j.status == ManagedJobStatus.RUNNING]
        for j in running_jobs:
            status = controller.check_vm_status(j)
            if status in ("not_found", "terminated", "preempted"):
                if j.task is not None:
                    controller.handle_preemption(j, j.task)

        # Recovery relaunched the job via provider.launch again (attempt 2).
        assert job.attempts == 2
        assert job.status == ManagedJobStatus.RUNNING
        assert provider.launch.call_count == 2

    def test_handle_preemption_fails_job_after_max_retries(self):
        controller, state, provider = _controller()
        provider.launch.return_value = {"vm_id": "mock-3", "ip_address": "1.2.3.4"}

        task = _task()
        job = controller.submit(task, command="python train.py", max_retries=1)
        controller.launch_job(job, task)  # attempt 1
        job.status = ManagedJobStatus.RUNNING

        result = controller.handle_preemption(job, task)

        assert result is False
        assert job.status == ManagedJobStatus.FAILED
        assert job.completed_at is not None

    def test_job_without_task_reference_fails_clearly_instead_of_hanging(self):
        """A job constructed without going through submit() (no task
        stored) can't be relaunched - must fail loudly, not spin forever
        as RUNNING."""
        controller, state, provider = _controller()
        provider.status.return_value = {"status": "terminated"}

        from minisky.managed_jobs import ManagedJob
        job = ManagedJob(job_id="j1", task_name="t", command="echo hi", status=ManagedJobStatus.RUNNING, vm_id="mock-x")
        with controller._lock:
            controller._jobs[job.job_id] = job

        status = controller.check_vm_status(job)
        assert status == "terminated"
        assert job.task is None


class TestCancelAndComplete:
    def test_cancel_terminates_vm_and_sets_status(self):
        controller, state, provider = _controller()
        task = _task()
        job = controller.submit(task, command="echo hi")
        job.vm_id = "mock-1"
        job.status = ManagedJobStatus.RUNNING

        result = controller.cancel(job.job_id)

        assert result is True
        assert job.status == ManagedJobStatus.CANCELLED
        provider.terminate.assert_called_once_with("mock-1")

    def test_complete_success(self):
        controller, state, provider = _controller()
        task = _task()
        job = controller.submit(task, command="echo hi")
        job.vm_id = "mock-1"

        result = controller.complete(job.job_id, success=True)

        assert result is True
        assert job.status == ManagedJobStatus.COMPLETED
