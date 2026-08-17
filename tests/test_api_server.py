"""
Tests for the ClusterController/JobController/EventBus business logic in
minisky/api/core.py, and for the thin FastAPI app in minisky/api/server.py
that delegates to them.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import minisky.api.core as core_module
from minisky.api.server import app
from minisky.api.core import (
    Event,
    EventBus,
    EventType,
    ClusterController,
    ClusterRecord,
    ClusterState,
    JobController,
    JobRecord,
    JobState,
)
from minisky.providers.mock import MockProvider
from minisky.providers.base import ProviderError
from minisky.executor import ExecutorError
from minisky.task import Task, ResourceRequirements
from minisky.state import StateManager


async def _wait_until(predicate, timeout=5.0, interval=0.05):
    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(interval)
        elapsed += interval


class TestEventBusTopic:
    @pytest.mark.asyncio
    async def test_publish_stamps_topic_and_serializes_it(self):
        bus = EventBus()
        queue = await bus.subscribe(topic="job:abc")

        event = Event(event_type=EventType.JOB_STATE_CHANGE, payload={"job_id": "abc"})
        await bus.publish(event, topic="job:abc")

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.topic == "job:abc"
        assert json.loads(received.to_json())["topic"] == "job:abc"

    @pytest.mark.asyncio
    async def test_global_subscriber_also_sees_stamped_topic(self):
        bus = EventBus()
        queue = await bus.subscribe()  # global, no topic filter

        event = Event(event_type=EventType.JOB_STATE_CHANGE, payload={})
        await bus.publish(event, topic="cluster:xyz")

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.topic == "cluster:xyz"


class TestEventBusConcurrentMutation:
    @pytest.mark.asyncio
    async def test_publish_serializes_against_concurrent_unsubscribe(self):
        """A subscriber disconnecting (unsubscribing) from a separate task
        while publish() is mid-flight in another task must not raise
        'RuntimeError: Set changed size during iteration', and must not
        deadlock - EventBus's lock means the unsubscribe simply waits its
        turn rather than racing the iteration."""
        bus = EventBus()
        other_queue = await bus.subscribe()

        class SlowQueue:
            def __init__(self):
                self.items = []

            async def put(self, item):
                # Give a concurrently-scheduled unsubscribe() a chance to
                # actually run (and prove it blocks on the lock, not race).
                await asyncio.sleep(0.05)
                self.items.append(item)

        slow_queue = SlowQueue()
        bus._global_subscribers.add(slow_queue)

        event = Event(event_type=EventType.ERROR, payload={})
        publish_task = asyncio.create_task(bus.publish(event))
        await asyncio.sleep(0.01)  # let publish() acquire the lock first
        unsub_task = asyncio.create_task(bus.unsubscribe(other_queue))

        await asyncio.wait_for(publish_task, timeout=2.0)
        await asyncio.wait_for(unsub_task, timeout=2.0)

        assert slow_queue.items == [event]
        assert other_queue not in bus._global_subscribers


class TestJobControllerFailureOldState:
    @pytest.mark.asyncio
    async def test_failure_emits_true_previous_state_not_failed_failed(self):
        bus = EventBus()
        events = []

        class RecordingQueue:
            async def put(self, item):
                events.append(item)

        bus._global_subscribers.add(RecordingQueue())

        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        job = JobRecord(
            job_id="job-x",
            name="n",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="run.sh",
            cluster_id="nonexistent-cluster",
        )
        await job_controller._execute_job(job)

        assert job.state == JobState.FAILED
        failure_events = [
            e for e in events
            if e.event_type == EventType.JOB_STATE_CHANGE and e.payload.get("new_state") == "failed"
        ]
        assert failure_events, "expected a job_state_change event for the failure"
        assert failure_events[-1].payload["old_state"] == "pending"


class TestClusterControllerMissingCluster:
    @pytest.mark.asyncio
    async def test_job_wait_loop_reports_clear_error_if_cluster_disappears(self):
        bus = EventBus()
        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        cluster = await cluster_controller.create_cluster(name="c", provider="mock")
        await cluster_controller._transition_state(cluster, ClusterState.LAUNCHING)

        # Simulate the cluster being terminated/removed while the job's
        # wait loop is polling it: first poll sees it LAUNCHING, the next
        # poll (after _do_terminate deletes the record) sees it gone.
        call_count = {"n": 0}

        def flaky_get_cluster(cluster_id):
            call_count["n"] += 1
            return cluster if call_count["n"] == 1 else None

        cluster_controller.get_cluster = flaky_get_cluster

        job = JobRecord(
            job_id="job-y",
            name="n",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="run.sh",
            cluster_id=cluster.cluster_id,
        )
        await job_controller._execute_job(job)

        assert job.state == JobState.FAILED
        assert "no longer exists" in job.failure_reason


class TestCORSConfig:
    def test_wildcard_origin_not_combined_with_credentials(self):
        """CORS spec forbids allow_origins=['*'] with allow_credentials=True -
        browsers reject the response outright when both are set."""
        cors_middleware = next(
            m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"
        )
        kwargs = cors_middleware.kwargs
        if kwargs.get("allow_origins") == ["*"]:
            assert kwargs.get("allow_credentials", False) is False


class TestClusterControllerRealProviderWiring:
    """
    ClusterController used to be pure theater - _do_launch just slept and
    hardcoded head_ip='10.0.0.1'. It now calls the real provider (mock
    here, so these tests run with no network/credentials) via
    get_provider(), so a cluster's state actually reflects what the
    provider did.
    """

    @pytest.mark.asyncio
    async def test_launch_calls_provider_and_reaches_up_with_real_vm_info(self, tmp_path):
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            cluster = await controller.create_cluster(
                name="c1", provider="mock", accelerators={"A100": 1}
            )
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

        assert cluster.state == ClusterState.UP
        assert cluster.vm_id is not None and cluster.vm_id.startswith("mock-")
        assert cluster.head_ip == "127.0.0.1"
        assert cluster.vm_id in provider._instances

    @pytest.mark.asyncio
    async def test_launch_with_autostop_spawns_watcher(self, tmp_path):
        """
        Regression test: a cluster launched via the dashboard/API with
        autostop_minutes set used to persist that field but never
        actually enforce it - unlike `minisky launch`, which spawns a
        detached watcher process. Verify _do_launch spawns the same
        watcher when autostop_minutes is set.
        """
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider), \
             patch.object(core_module, "spawn_autostop_watcher") as mock_spawn:
            cluster = await controller.create_cluster(
                name="c-autostop", provider="mock", accelerators={"A100": 1}, autostop_minutes=15
            )
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

        assert cluster.state == ClusterState.UP
        mock_spawn.assert_called_once_with(cluster.vm_id, 15, controller.config)

    @pytest.mark.asyncio
    async def test_launch_without_autostop_does_not_spawn_watcher(self, tmp_path):
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider), \
             patch.object(core_module, "spawn_autostop_watcher") as mock_spawn:
            cluster = await controller.create_cluster(name="c-no-autostop", provider="mock", accelerators={"A100": 1})
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

        assert cluster.state == ClusterState.UP
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_launch_failure_from_provider_transitions_to_error(self):
        bus = EventBus()
        controller = ClusterController(bus)
        events = []

        class RecordingQueue:
            async def put(self, item):
                events.append(item)

        bus._global_subscribers.add(RecordingQueue())

        class FailingProvider(MockProvider):
            def launch(self, task):
                raise ProviderError("simulated capacity error")

        with patch.object(core_module, "get_provider", return_value=FailingProvider()):
            cluster = await controller.create_cluster(
                name="c2", provider="mock", accelerators={"A100": 1}
            )
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

        assert cluster.state == ClusterState.ERROR
        error_events = [e for e in events if e.payload.get("new_state") == "error"]
        assert error_events
        assert "simulated capacity error" in error_events[-1].payload["reason"]

    @pytest.mark.asyncio
    async def test_stop_and_terminate_call_through_to_the_same_provider(self, tmp_path):
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            cluster = await controller.create_cluster(
                name="c3", provider="mock", accelerators={"T4": 1}
            )
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)
            assert cluster.state == ClusterState.UP
            vm_id = cluster.vm_id

            await controller.stop_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.STOPPING)
            assert cluster.state == ClusterState.STOPPED
            assert provider._instances[vm_id]["status"] == "stopped"

            await controller.terminate_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.cluster_id not in controller._clusters)

        assert vm_id not in provider._instances


class TestCliDashboardClusterUnification:
    """
    ClusterController used to be a completely separate world from
    `minisky launch`/`minisky cluster launch`: it had its own `clusters`
    persistence table, and never touched state.py's `vms` table (the one
    minisky status/cost-report/terminate all read) either way. A VM
    launched via the CLI was invisible to the dashboard, and a cluster
    created via the dashboard was invisible to the CLI. These tests
    cover the fix: ClusterController now reads state.py's vms table as a
    second source of clusters (source="cli"), and writes to it when it
    launches a VM itself (source="api") so the CLI can see those too.
    """

    def test_cli_launched_vm_appears_via_list_clusters(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-abc12345",
            "ip_address": "127.0.0.1",
            "status": "running",
            "provider": "mock",
            "task_name": "my-task",
        })
        controller = ClusterController(EventBus(), state=state)

        clusters = controller.list_clusters()

        assert len(clusters) == 1
        c = clusters[0]
        assert c.cluster_id == "mock-abc12345"
        assert c.source == "cli"
        assert c.state == ClusterState.UP
        assert c.vm_id == "mock-abc12345"
        assert c.num_nodes == 1

    def test_cli_multi_node_cluster_groups_by_cluster_id(self, tmp_path):
        """Mirrors cluster.py's ClusterManager convention: VMs sharing a
        cluster_id tag (from `minisky cluster launch`) form one cluster,
        head node identified by node_role."""
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-head01", "ip_address": "10.0.0.1", "status": "running",
            "provider": "mock", "task_name": "train",
            "cluster_id": "cluster-xyz", "node_role": "head",
        })
        state.add_vm({
            "vm_id": "mock-work01", "ip_address": "10.0.0.2", "status": "running",
            "provider": "mock", "task_name": "train",
            "cluster_id": "cluster-xyz", "node_role": "worker", "rank": 1,
        })
        controller = ClusterController(EventBus(), state=state)

        clusters = controller.list_clusters()

        assert len(clusters) == 1
        c = clusters[0]
        assert c.cluster_id == "cluster-xyz"
        assert c.num_nodes == 2
        assert c.vm_id == "mock-head01"
        assert c.worker_vm_ids == ["mock-work01"]
        assert c.worker_ips == ["10.0.0.2"]

    def test_terminated_vm_not_listed(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-gone", "ip_address": "1.2.3.4", "status": "running",
            "provider": "mock", "task_name": "t",
        })
        state.remove_vm("mock-gone")
        controller = ClusterController(EventBus(), state=state)

        assert controller.list_clusters() == []

    def test_get_cluster_resolves_a_cli_vm(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-abc12345", "ip_address": "127.0.0.1", "status": "stopped",
            "provider": "mock", "task_name": "t",
        })
        controller = ClusterController(EventBus(), state=state)

        cluster = controller.get_cluster("mock-abc12345")

        assert cluster is not None
        assert cluster.state == ClusterState.STOPPED

    def test_get_cluster_unknown_id_returns_none(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        controller = ClusterController(EventBus(), state=state)
        assert controller.get_cluster("nonexistent") is None

    @pytest.mark.asyncio
    async def test_api_launched_cluster_appears_in_state_vms(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        controller = ClusterController(EventBus(), state=state)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            cluster = await controller.create_cluster(name="c1", provider="mock", accelerators={"A100": 1})
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)

        vms = state.list_vms()
        assert len(vms) == 1
        assert vms[0]["vm_id"] == cluster.vm_id
        assert vms[0]["cluster_id"] == cluster.cluster_id
        assert vms[0]["node_role"] == "head"

    @pytest.mark.asyncio
    async def test_dashboard_terminate_removes_cli_vm_from_state(self, tmp_path):
        """
        Regression coverage: terminating a cluster via the dashboard must
        remove the VM from state.py too, not just the API's own clusters
        table - otherwise `minisky status` keeps showing a VM that's
        actually gone (and, before source-tagging existed, a cluster
        created via the dashboard never wrote to state.py at all, so
        this couldn't have been tested before it could even matter).
        """
        state = StateManager(db_path=str(tmp_path / "state.db"))
        controller = ClusterController(EventBus(), state=state)
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})

        with patch.object(core_module, "get_provider", return_value=provider):
            cluster = await controller.create_cluster(name="c1", provider="mock", accelerators={"A100": 1})
            await controller.launch_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.state != ClusterState.LAUNCHING)
            assert len(state.list_vms()) == 1

            await controller.terminate_cluster(cluster.cluster_id)
            await _wait_until(lambda: cluster.cluster_id not in controller._clusters)

        assert state.list_vms() == []

    @pytest.mark.asyncio
    async def test_stop_via_dashboard_adopts_cli_cluster_and_updates_state(self, tmp_path):
        """
        A `minisky launch`'d VM stopped via the dashboard must actually
        get stopped (real provider call) and reflected in state.py, and
        from then on be tracked as a normal (adopted) API cluster - not
        re-synthesized (and not double-listed) on the next list_clusters().
        """
        state = StateManager(db_path=str(tmp_path / "state.db"))
        provider = MockProvider({"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})
        task = Task(name="cli-task", run=["echo hi"], resources=ResourceRequirements(gpu="A100"))
        vm_info = provider.launch(task)
        state.add_vm(vm_info)

        controller = ClusterController(EventBus(), state=state)

        with patch.object(core_module, "get_provider", return_value=provider):
            await controller.stop_cluster(vm_info["vm_id"])
            await _wait_until(
                lambda: controller._clusters.get(vm_info["vm_id"]) is not None
                and controller._clusters[vm_info["vm_id"]].state != ClusterState.STOPPING
            )

        assert controller._clusters[vm_info["vm_id"]].state == ClusterState.STOPPED
        assert provider._instances[vm_info["vm_id"]]["status"] == "stopped"
        assert state.get_vm(vm_info["vm_id"])["status"] == "stopped"

        # Adopted now - must appear exactly once (not re-synthesized as a
        # second, duplicate entry alongside the now-tracked one). source
        # stays "cli": adoption makes it trackable/operable from the
        # dashboard, but it's still honestly a CLI-launched VM in origin.
        clusters = controller.list_clusters()
        matching = [c for c in clusters if c.cluster_id == vm_info["vm_id"]]
        assert len(matching) == 1
        assert matching[0].source == "cli"


def _up_cluster(cluster_id="sky-fixed01", provider="mock"):
    return ClusterRecord(
        cluster_id=cluster_id,
        name="c",
        state=ClusterState.UP,
        provider=provider,
        head_ip="203.0.113.9",
        ssh_port=22,
        ssh_user="ubuntu",
        vm_id="mock-fixed01",
        launched_at=datetime.utcnow(),
    )


class TestJobExecutionRealSSH:
    """
    _execute_job used to simulate everything with asyncio.sleep(). It now
    actually SSHes into the cluster's VM via Executor.execute_task() -
    these tests mock out Executor itself (no real network/SSH) and assert
    it's called with the cluster's real connection info and the job's
    entrypoint as the run command.
    """

    @pytest.mark.asyncio
    async def test_execute_job_runs_entrypoint_via_executor_over_real_vm_info(self):
        bus = EventBus()
        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        cluster = _up_cluster()
        cluster_controller._clusters[cluster.cluster_id] = cluster

        mock_executor_instance = MagicMock()
        mock_executor_cls = MagicMock(return_value=mock_executor_instance)

        job = JobRecord(
            job_id="job-1",
            name="train",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="python train.py",
            cluster_id=cluster.cluster_id,
        )

        with patch.object(core_module, "Executor", mock_executor_cls):
            await job_controller._execute_job(job)

        assert job.state == JobState.SUCCEEDED
        assert job.exit_code == 0

        mock_executor_cls.assert_called_once_with({
            "ip_address": "203.0.113.9",
            "ssh_port": 22,
            "ssh_user": "ubuntu",
        })
        called_task = mock_executor_instance.execute_task.call_args.args[0]
        assert called_task.run == ["python train.py"]
        assert called_task.provider == "mock"

        # execute_task must also have been handed a live on_line callback
        assert callable(mock_executor_instance.execute_task.call_args.kwargs.get("on_line"))

    @pytest.mark.asyncio
    async def test_log_lines_are_published_as_events_while_executing(self):
        """Executor's on_line callback fires from a worker thread
        (asyncio.to_thread) - verifies it correctly crosses back onto the
        event loop via run_coroutine_threadsafe and reaches a job:<id>
        topic subscriber as real LOG_LINE events, not just after the job
        finishes."""
        bus = EventBus()
        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        cluster = _up_cluster(cluster_id="sky-fixed03")
        cluster_controller._clusters[cluster.cluster_id] = cluster

        def fake_execute_task(task, on_line=None):
            on_line("python train.py", "command")
            on_line("epoch 1/10 - loss 0.42", "stdout")

        mock_executor_instance = MagicMock()
        mock_executor_instance.execute_task.side_effect = fake_execute_task
        mock_executor_cls = MagicMock(return_value=mock_executor_instance)

        job = JobRecord(
            job_id="job-log-1",
            name="train",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="python train.py",
            cluster_id=cluster.cluster_id,
        )

        queue = await bus.subscribe(topic=f"job:{job.job_id}")

        with patch.object(core_module, "Executor", mock_executor_cls):
            await job_controller._execute_job(job)

        assert job.state == JobState.SUCCEEDED

        # on_line runs cross-thread via run_coroutine_threadsafe, so give
        # the loop a moment to actually process the scheduled publishes
        await _wait_until(lambda: queue.qsize() >= 2, timeout=2.0)

        log_events = []
        while not queue.empty():
            event = queue.get_nowait()
            if event.event_type == EventType.LOG_LINE:
                log_events.append(event)

        lines = [e.payload["line"] for e in log_events]
        streams = [e.payload["stream"] for e in log_events]
        assert "python train.py" in lines
        assert "epoch 1/10 - loss 0.42" in lines
        assert "command" in streams
        assert "stdout" in streams
        assert all(e.payload["job_id"] == job.job_id for e in log_events)
        assert all(e.topic == f"job:{job.job_id}" for e in log_events)

    @pytest.mark.asyncio
    async def test_executor_error_reported_as_job_failure(self):
        bus = EventBus()
        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        cluster = _up_cluster(cluster_id="sky-fixed02")
        cluster_controller._clusters[cluster.cluster_id] = cluster

        mock_executor_instance = MagicMock()
        mock_executor_instance.execute_task.side_effect = ExecutorError(
            "setup command failed with exit code 1"
        )
        mock_executor_cls = MagicMock(return_value=mock_executor_instance)

        job = JobRecord(
            job_id="job-2",
            name="train",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="python train.py",
            cluster_id=cluster.cluster_id,
        )

        with patch.object(core_module, "Executor", mock_executor_cls):
            await job_controller._execute_job(job)

        assert job.state == JobState.FAILED
        assert "setup command failed" in job.failure_reason

    @pytest.mark.asyncio
    async def test_job_without_cluster_id_fails_clearly_instead_of_faking_success(self):
        bus = EventBus()
        cluster_controller = ClusterController(bus)
        job_controller = JobController(bus, cluster_controller)

        job = JobRecord(
            job_id="job-3",
            name="train",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="python train.py",
            cluster_id=None,
        )

        await job_controller._execute_job(job)

        assert job.state == JobState.FAILED
        assert "cluster_id is required" in job.failure_reason


class TestBuildJobTask:
    def test_entrypoint_is_authoritative_run_command(self):
        cluster = _up_cluster()
        job = JobRecord(
            job_id="j",
            name="train",
            state=JobState.PENDING,
            task_yaml=(
                "run: ['this should be ignored']\n"
                "setup: ['pip install -r requirements.txt']\n"
                "env:\n  FOO: bar\n"
            ),
            entrypoint="python train.py",
        )
        task = JobController._build_job_task(job, cluster)
        assert task.run == ["python train.py"]
        assert task.setup == ["pip install -r requirements.txt"]
        assert task.env == {"FOO": "bar"}

    def test_empty_task_yaml_is_fine(self):
        cluster = _up_cluster()
        job = JobRecord(
            job_id="j",
            name="train",
            state=JobState.PENDING,
            task_yaml="",
            entrypoint="python train.py",
        )
        task = JobController._build_job_task(job, cluster)
        assert task.run == ["python train.py"]
        assert task.setup is None


class TestClusterResponseSerialization:
    def test_instance_type_accelerators_autostop_survive_the_response(self):
        """ClusterResponse used to drop instance_type/accelerators/
        autostop_minutes entirely, so a caller reading them back off the
        SDK's Cluster model always saw None regardless of what they
        submitted at creation."""
        from fastapi.testclient import TestClient
        from minisky.api.server import app

        with TestClient(app) as client:
            r = client.post("/v1/clusters", json={
                "name": "t",
                "provider": "mock",
                "accelerators": {"A100": 2},
                "autostop_minutes": 30,
            })
            data = r.json()

        assert data["accelerators"] == {"A100": 2}
        assert data["autostop_minutes"] == 30


class TestDashboardStaticServing:
    """
    Tests for serving the built Vue dashboard (dashboard/dist) from the
    same FastAPI process. useApi.js calls unprefixed paths like
    /v1/clusters directly (no /api indirection) - this only works in
    production if the same server that answers those API routes also
    serves the dashboard's static files and falls back to index.html
    for Vue Router's client-side routes.
    """

    def _client(self):
        from fastapi.testclient import TestClient
        from minisky.api.server import app
        return TestClient(app)

    def test_not_built_returns_404_with_helpful_message(self, tmp_path):
        from minisky.api import server as server_module

        with patch.object(server_module, "DASHBOARD_DIST", tmp_path / "does-not-exist"):
            r = self._client().get("/")

        assert r.status_code == 404
        assert "not built" in r.json()["detail"].lower()

    def test_serves_index_html_at_root(self, tmp_path):
        from minisky.api import server as server_module

        (tmp_path / "index.html").write_text("<html>dashboard</html>")

        with patch.object(server_module, "DASHBOARD_DIST", tmp_path):
            r = self._client().get("/")

        assert r.status_code == 200
        assert "dashboard" in r.text

    def test_spa_route_falls_back_to_index_html(self, tmp_path):
        """Vue Router uses history mode - a direct request for a
        client-side route like /clusters/abc123 isn't a real file, so it
        must fall back to index.html and let Vue Router resolve it."""
        from minisky.api import server as server_module

        (tmp_path / "index.html").write_text("<html>dashboard</html>")

        with patch.object(server_module, "DASHBOARD_DIST", tmp_path):
            r = self._client().get("/clusters/abc123")

        assert r.status_code == 200
        assert "dashboard" in r.text

    def test_real_static_asset_is_served_directly(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>dashboard</html>")
        assets_dir = tmp_path / "assets"
        assets_dir.mkdir()
        (assets_dir / "app.js").write_text("console.log('hi')")

        from minisky.api import server as server_module

        with patch.object(server_module, "DASHBOARD_DIST", tmp_path):
            r = self._client().get("/assets/app.js")

        assert r.status_code == 200
        assert "console.log" in r.text

    def test_api_routes_are_not_shadowed_by_the_catch_all(self, tmp_path):
        """/health and /v1/* must keep hitting their real handlers, not
        the dashboard catch-all, even when a dashboard build exists."""
        (tmp_path / "index.html").write_text("<html>dashboard</html>")

        from minisky.api import server as server_module

        with patch.object(server_module, "DASHBOARD_DIST", tmp_path):
            r = self._client().get("/health")

        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_path_traversal_cannot_escape_dist_directory(self, tmp_path):
        """
        A crafted path must not be able to read files outside
        DASHBOARD_DIST. Calls the route function directly with a raw
        full_path containing '..' segments - going through TestClient/
        httpx wouldn't actually exercise this: httpx normalizes '..' out
        of the URL client-side before the request is even sent, so a
        request through the client can never reach the handler with
        those segments still in full_path regardless of whether the
        server-side guard works.
        """
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>dashboard</html>")
        secret = tmp_path / "secret.txt"
        secret.write_text("do not serve me")

        from minisky.api import server as server_module

        with patch.object(server_module, "DASHBOARD_DIST", dist):
            response = await server_module.serve_dashboard("../secret.txt")

        # FileResponse resolves the file to serve into .path; assert it's
        # index.html, not the secret file outside DASHBOARD_DIST.
        assert Path(response.path) == dist / "index.html"
