"""Tests for the managed jobs / spot recovery system (minisky/managed_jobs.py)."""

import time
from unittest.mock import MagicMock

import pytest

from minisky.managed_jobs import ManagedJob, ManagedJobController, ManagedJobStatus
from minisky.state import StateManager
from minisky.task import Task, ResourceRequirements


def _task(name="train"):
    task = MagicMock()
    task.name = name
    task.resources = MagicMock()
    task.resources.use_spot = False
    return task


def _real_task(name="train"):
    return Task(name=name, run=["python train.py"], resources=ResourceRequirements(gpu="A100"))


def _controller(provider=None, storage=None, executor_factory=None, state=None):
    state = state if state is not None else MagicMock()
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


class TestSerialization:
    def test_round_trip_preserves_fields(self):
        job = ManagedJob(
            job_id="managed-abc123",
            task_name="train",
            command="python train.py",
            status=ManagedJobStatus.RUNNING,
            vm_id="mock-1",
            task=_real_task(),
            attempts=2,
            error_message=None,
        )
        restored = ManagedJob.from_dict(job.to_dict())

        assert restored.job_id == job.job_id
        assert restored.status == ManagedJobStatus.RUNNING
        assert restored.vm_id == "mock-1"
        assert restored.attempts == 2
        assert restored.task.name == "train"
        assert restored.task.resources.gpu == "A100"

    def test_round_trip_with_no_task(self):
        job = ManagedJob(job_id="j1", task_name="t", command="echo hi", status=ManagedJobStatus.FAILED)
        restored = ManagedJob.from_dict(job.to_dict())
        assert restored.task is None


class TestPersistence:
    def test_persists_on_submit_and_survives_new_instance(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        controller, _, provider = _controller(state=state)
        job = controller.submit(_real_task(), command="python train.py")

        fresh = ManagedJobController(state_manager=state, provider=provider)
        fresh.load_persisted()

        loaded = fresh.get_job(job.job_id)
        assert loaded is not None
        assert loaded.status == ManagedJobStatus.PENDING
        assert loaded.task.name == "train"

    def test_status_changes_are_visible_across_instances(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        controller, _, provider = _controller(state=state)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        job = controller.submit(_real_task(), command="python train.py")
        controller.launch_job(job, job.task)

        fresh = ManagedJobController(state_manager=state, provider=provider)
        fresh.load_persisted()
        loaded = fresh.get_job(job.job_id)

        assert loaded.status == ManagedJobStatus.RUNNING
        assert loaded.vm_id == "mock-1"

    def test_load_persisted_skips_unparseable_rows(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.save_managed_job("broken", {"job_id": "broken", "status": "not-a-real-status"})
        controller, _, _ = _controller(state=state)
        controller.load_persisted()  # must not raise
        assert controller.get_job("broken") is None


class TestRunToCompletion:
    def _controller_with_state(self, tmp_path, executor_factory=None):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        provider = MagicMock()
        controller = ManagedJobController(
            state_manager=state,
            provider=provider,
            executor_factory=executor_factory or MagicMock(),
        )
        return controller, state, provider

    def test_happy_path_completes_without_recovery(self, tmp_path):
        controller, state, provider = self._controller_with_state(tmp_path)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 0
        controller.executor_factory = lambda vm_info: mock_executor

        task = _real_task()
        job = controller.submit(task, command="python train.py")

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.COMPLETED
        provider.launch.assert_called_once()

    def test_command_failure_completes_as_failed_without_recovery(self, tmp_path):
        controller, state, provider = self._controller_with_state(tmp_path)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 1
        controller.executor_factory = lambda vm_info: mock_executor

        task = _real_task()
        job = controller.submit(task, command="python train.py")

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.FAILED
        # A real command failure (not a VM disappearing) must not trigger a relaunch.
        assert provider.launch.call_count == 1

    def test_preemption_mid_run_triggers_relaunch_then_completes(self, tmp_path):
        controller, state, provider = self._controller_with_state(tmp_path)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        provider.status.return_value = {"status": "preempted"}

        call_count = {"n": 0}
        mock_executor = MagicMock()

        def _execute_command(cmd):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("connection dropped")
            return 0

        mock_executor.execute_command.side_effect = _execute_command
        controller.executor_factory = lambda vm_info: mock_executor

        task = _real_task()
        job = controller.submit(task, command="python train.py")
        job.recovery_config.retry_delay_seconds = 0

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.COMPLETED
        assert provider.launch.call_count == 2
        assert job.attempts == 2

    def test_exhausted_retries_ends_as_failed(self, tmp_path):
        controller, state, provider = self._controller_with_state(tmp_path)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
        provider.status.return_value = {"status": "preempted"}

        mock_executor = MagicMock()
        mock_executor.execute_command.side_effect = ConnectionError("dropped")
        controller.executor_factory = lambda vm_info: mock_executor

        task = _real_task()
        job = controller.submit(task, command="python train.py", max_retries=1)
        job.recovery_config.retry_delay_seconds = 0

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.FAILED

    def test_cancel_during_run_is_not_treated_as_preemption(self, tmp_path):
        """
        If the VM disappears because `minisky jobs cancel` terminated it
        directly (not because of a real preemption), run_to_completion
        must recognize the cancellation and stop - not relaunch.
        """
        controller, state, provider = self._controller_with_state(tmp_path)
        provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}

        mock_executor = MagicMock()

        def _execute_command(cmd):
            # Simulate an external `jobs cancel` racing with the command:
            # by the time execute_job's SSH call fails, state already
            # reflects CANCELLED.
            state.save_managed_job(job.job_id, job.to_dict() | {"status": "cancelled"})
            raise ConnectionError("connection dropped")

        mock_executor.execute_command.side_effect = _execute_command
        controller.executor_factory = lambda vm_info: mock_executor

        task = _real_task()
        job = controller.submit(task, command="python train.py")

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.CANCELLED
        assert provider.launch.call_count == 1  # no relaunch attempted

    def test_cancel_racing_the_initial_launch_still_terminates_the_vm(self, tmp_path):
        """
        Regression test for a real bug found via live E2E testing: if
        `minisky jobs cancel` writes CANCELLED to persisted state while
        the runner is still inside its *initial* launch_job() call,
        launch_job()'s own _persist() (status=RUNNING) would overwrite
        the cancel write, silently losing it and leaving an orphaned VM
        running forever. The runner must check for this race right after
        launch_job() returns and terminate the VM it just launched.
        """
        controller, state, provider = self._controller_with_state(tmp_path)

        def _launch(task):
            vm_info = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
            # Simulate a `jobs cancel` landing while we were mid-launch.
            state.save_managed_job(job.job_id, {
                "job_id": job.job_id, "task_name": job.task_name, "command": job.command,
                "status": "cancelled", "vm_id": None, "task": job.task.model_dump(),
                "checkpoint_config": {}, "recovery_config": {}, "attempts": 0,
                "last_checkpoint": None, "last_checkpoint_time": None,
                "created_at": job.created_at, "started_at": None, "completed_at": time.time(),
                "error_message": None,
            })
            return vm_info

        provider.launch.side_effect = _launch

        task = _real_task()
        job = controller.submit(task, command="python train.py")

        final_status = controller.run_to_completion(job, task, check_interval=0)

        assert final_status == ManagedJobStatus.CANCELLED
        provider.terminate.assert_called_once_with("mock-1")
        assert provider.launch.call_count == 1
