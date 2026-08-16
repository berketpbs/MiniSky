"""
Tests for minisky/api/server.py - the FastAPI app actually served by
`minisky serve` (distinct from minisky/api/core.py, which is a separate,
parallel implementation covered by tests/test_api.py).
"""

import asyncio
import json
from unittest.mock import patch

import pytest

import minisky.api.server as server_module
from minisky.api.server import (
    app,
    Event,
    EventBus,
    EventType,
    ClusterController,
    ClusterState,
    JobController,
    JobRecord,
    JobState,
)
from minisky.providers.mock import MockProvider
from minisky.providers.base import ProviderError


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
        queue = bus.subscribe(topic="job:abc")

        event = Event(event_type=EventType.JOB_STATE_CHANGE, payload={"job_id": "abc"})
        await bus.publish(event, topic="job:abc")

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.topic == "job:abc"
        assert json.loads(received.to_json())["topic"] == "job:abc"

    @pytest.mark.asyncio
    async def test_global_subscriber_also_sees_stamped_topic(self):
        bus = EventBus()
        queue = bus.subscribe()  # global, no topic filter

        event = Event(event_type=EventType.JOB_STATE_CHANGE, payload={})
        await bus.publish(event, topic="cluster:xyz")

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.topic == "cluster:xyz"


class TestEventBusConcurrentMutation:
    @pytest.mark.asyncio
    async def test_publish_survives_unsubscribe_during_iteration(self):
        """A subscriber unsubscribing (e.g. a client disconnecting) while
        publish() is mid-iteration must not raise
        'RuntimeError: Set changed size during iteration'."""
        bus = EventBus()
        other_queue = bus.subscribe()

        class TriggeringQueue:
            def __init__(self):
                self.items = []

            async def put(self, item):
                bus.unsubscribe(other_queue)
                self.items.append(item)

        trigger_queue = TriggeringQueue()
        bus._global_subscribers.add(trigger_queue)

        event = Event(event_type=EventType.ERROR, payload={})
        await bus.publish(event)  # must not raise

        assert trigger_queue.items == [event]
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
    async def test_launch_calls_provider_and_reaches_up_with_real_vm_info(self):
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False})

        with patch.object(server_module, "get_provider", return_value=provider):
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

        with patch.object(server_module, "get_provider", return_value=FailingProvider()):
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
    async def test_stop_and_terminate_call_through_to_the_same_provider(self):
        bus = EventBus()
        controller = ClusterController(bus)
        provider = MockProvider({"simulate_delay": False})

        with patch.object(server_module, "get_provider", return_value=provider):
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
