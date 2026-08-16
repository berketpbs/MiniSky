"""
Tests for ClusterController/JobController persisting to StateManager
(minisky/api/core.py), so cluster/job records survive a server restart.
"""

from unittest.mock import patch

import pytest

import minisky.api.core as core_module
from minisky.api.core import (
    ClusterController,
    ClusterRecord,
    ClusterState,
    EventBus,
    JobController,
    JobRecord,
    JobState,
)
from minisky.providers.mock import MockProvider
from minisky.state import StateManager


async def _wait_until(predicate, timeout=5.0, interval=0.05):
    import asyncio
    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(interval)
        elapsed += interval


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


class TestClusterPersistenceAcrossRestart:
    @pytest.mark.asyncio
    async def test_cluster_survives_a_fresh_controller_pointed_at_the_same_state(self, tmp_path):
        state = _state(tmp_path)
        bus = EventBus()
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            controller = ClusterController(bus, state=state)
            cluster = await controller.create_cluster(name="c1", provider="mock", accelerators={"A100": 1})
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)
            assert cluster.state == ClusterState.UP

        # Simulate a server restart: brand new controller, same StateManager.
        restarted = ClusterController(EventBus(), state=state)

        reloaded = restarted.get_cluster(cluster.cluster_id)
        assert reloaded is not None
        assert reloaded.state == ClusterState.UP
        assert reloaded.vm_id == cluster.vm_id
        assert reloaded.head_ip == cluster.head_ip

    def test_in_flight_cluster_marked_error_on_reload(self, tmp_path):
        """A cluster caught mid-LAUNCHING when the process died has no
        background task to finish the launch anymore - its real status is
        unknown, so it must not be loaded back looking like work is still
        happening."""
        state = _state(tmp_path)
        stuck = ClusterRecord(
            cluster_id="sky-stuck",
            name="c",
            state=ClusterState.LAUNCHING,
            provider="mock",
        )
        state.save_cluster(stuck.cluster_id, {
            "cluster_id": stuck.cluster_id, "name": "c", "state": "launching",
            "provider": "mock", "region": None, "num_nodes": 1, "instance_type": None,
            "accelerators": None, "head_ip": None, "worker_ips": [], "ssh_port": 22,
            "ssh_user": None, "vm_id": None, "launched_at": None, "last_use": None,
            "autostop_minutes": None, "cost_per_hour": 0.0, "total_cost": 0.0,
            "task_yaml": None, "user_metadata": {},
        })

        controller = ClusterController(EventBus(), state=state)

        reloaded = controller.get_cluster("sky-stuck")
        assert reloaded.state == ClusterState.ERROR

    @pytest.mark.asyncio
    async def test_terminated_cluster_removed_from_persistence(self, tmp_path):
        state = _state(tmp_path)
        bus = EventBus()
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            controller = ClusterController(bus, state=state)
            cluster = await controller.create_cluster(name="c1", provider="mock", accelerators={"T4": 1})
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

            await controller.terminate_cluster(cluster.cluster_id)
            await _wait_until(lambda: state.get_cluster_data(cluster.cluster_id) is None)

        assert state.get_cluster_data(cluster.cluster_id) is None


class TestJobPersistenceAcrossRestart:
    def test_job_survives_a_fresh_controller_pointed_at_the_same_state(self, tmp_path):
        state = _state(tmp_path)
        bus = EventBus()
        cluster_controller = ClusterController(bus, state=state)
        job_controller = JobController(bus, cluster_controller, state=state)

        job = JobRecord(
            job_id="job-1", name="train", state=JobState.SUCCEEDED,
            task_yaml="", entrypoint="python train.py", exit_code=0,
        )
        job_controller._jobs[job.job_id] = job
        job_controller._persist_job(job)

        restarted = JobController(EventBus(), cluster_controller, state=state)
        reloaded = restarted.get_job("job-1")

        assert reloaded is not None
        assert reloaded.state == JobState.SUCCEEDED
        assert reloaded.exit_code == 0

    def test_in_flight_job_marked_failed_on_reload(self, tmp_path):
        """A job caught mid-RUNNING when the process died has no way to
        know whether the remote command actually succeeded - it must not
        be loaded back looking like it's still executing."""
        state = _state(tmp_path)
        bus = EventBus()
        cluster_controller = ClusterController(bus, state=state)

        state.save_job("job-stuck", {
            "job_id": "job-stuck", "name": "train", "state": "running",
            "task_yaml": "", "entrypoint": "python train.py", "cluster_id": None,
            "pid": None, "submitted_at": "2024-01-01T00:00:00", "started_at": None,
            "ended_at": None, "spot_recovery": False, "max_restarts": 0,
            "restart_count": 0, "log_path": None, "exit_code": None, "failure_reason": None,
        })

        job_controller = JobController(EventBus(), cluster_controller, state=state)
        reloaded = job_controller.get_job("job-stuck")

        assert reloaded.state == JobState.FAILED
        assert reloaded.failure_reason is not None
