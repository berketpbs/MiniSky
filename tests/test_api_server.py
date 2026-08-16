"""
Tests for minisky/api/server.py - the FastAPI app actually served by
`minisky serve` (distinct from minisky/api/core.py, which is a separate,
parallel implementation covered by tests/test_api.py).
"""

import asyncio
import json

import pytest

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
