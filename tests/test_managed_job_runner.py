"""Tests for the standalone managed-job runner entry point (minisky/managed_job_runner.py)."""

from unittest.mock import patch, MagicMock

import pytest

from minisky.managed_job_runner import main
from minisky.state import StateManager
from minisky.task import Task, ResourceRequirements


@pytest.fixture
def state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


def test_job_not_found_returns_error(state, tmp_path):
    with patch("minisky.managed_job_runner.StateManager", return_value=state), \
         patch("minisky.managed_job_runner.MiniSkyConfig"):
        exit_code = main(["managed-nonexistent"])
    assert exit_code == 1


def test_job_without_task_returns_error(state):
    state.save_managed_job("managed-abc", {
        "job_id": "managed-abc",
        "task_name": "t",
        "command": "echo hi",
        "status": "pending",
        "vm_id": None,
        "task": None,
        "checkpoint_config": {},
        "recovery_config": {},
        "attempts": 0,
        "last_checkpoint": None,
        "last_checkpoint_time": None,
        "created_at": 0.0,
        "started_at": None,
        "completed_at": None,
        "error_message": None,
    })

    with patch("minisky.managed_job_runner.StateManager", return_value=state), \
         patch("minisky.managed_job_runner.MiniSkyConfig"):
        exit_code = main(["managed-abc"])
    assert exit_code == 1


def test_runs_job_to_completion(state):
    task = Task(name="t", run=["echo hi"], resources=ResourceRequirements(gpu="A100"))
    state.save_managed_job("managed-xyz", {
        "job_id": "managed-xyz",
        "task_name": "t",
        "command": "echo hi",
        "status": "pending",
        "vm_id": None,
        "task": task.model_dump(),
        "checkpoint_config": {},
        "recovery_config": {},
        "attempts": 0,
        "last_checkpoint": None,
        "last_checkpoint_time": None,
        "created_at": 0.0,
        "started_at": None,
        "completed_at": None,
        "error_message": None,
    })

    mock_provider = MagicMock()
    mock_provider.launch.return_value = {"vm_id": "mock-1", "ip_address": "1.2.3.4"}
    mock_executor = MagicMock()
    mock_executor.execute_command.return_value = 0

    with patch("minisky.managed_job_runner.StateManager", return_value=state), \
         patch("minisky.managed_job_runner.MiniSkyConfig"), \
         patch("minisky.managed_job_runner.get_provider", return_value=mock_provider), \
         patch("minisky.managed_job_runner.Executor", return_value=mock_executor), \
         patch("minisky.managed_job_runner.StorageManager"):
        exit_code = main(["managed-xyz", "--check-interval-seconds", "0"])

    assert exit_code == 0
    persisted = state.get_managed_job_data("managed-xyz")
    assert persisted["status"] == "completed"
