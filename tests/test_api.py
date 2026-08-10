"""
Tests for the API core module.

Covers ClusterController state machine, EventBus pub/sub,
JobState mappings, and provider registry.
"""

import pytest
import asyncio
from minisky.api.core import (
    ClusterState,
    JobState,
    ClusterRecord,
    JobRecord,
    EventBus,
    Event,
    EventType,
    ClusterController,
    ProviderRegistry,
)
from minisky.state import StateManager
from minisky.queue import JobStatus


# ---------------------------------------------------------------------------
# ClusterState tests
# ---------------------------------------------------------------------------

class TestClusterState:
    """Test ClusterState enum and mappings."""

    def test_from_vm_status_running(self):
        assert ClusterState.from_vm_status("running") == ClusterState.UP

    def test_from_vm_status_stopped(self):
        assert ClusterState.from_vm_status("stopped") == ClusterState.STOPPED

    def test_from_vm_status_terminated(self):
        assert ClusterState.from_vm_status("terminated") == ClusterState.TERMINATED

    def test_from_vm_status_unknown(self):
        assert ClusterState.from_vm_status("weird_status") == ClusterState.ERROR


# ---------------------------------------------------------------------------
# JobState tests
# ---------------------------------------------------------------------------

class TestJobState:
    """Test JobState enum and mappings."""

    def test_from_job_status_pending(self):
        assert JobState.from_job_status(JobStatus.PENDING) == JobState.PENDING

    def test_from_job_status_running(self):
        assert JobState.from_job_status(JobStatus.RUNNING) == JobState.RUNNING

    def test_from_job_status_completed(self):
        assert JobState.from_job_status(JobStatus.COMPLETED) == JobState.SUCCEEDED

    def test_from_job_status_failed(self):
        assert JobState.from_job_status(JobStatus.FAILED) == JobState.FAILED

    def test_from_job_status_cancelled(self):
        assert JobState.from_job_status(JobStatus.CANCELLED) == JobState.CANCELLED


# ---------------------------------------------------------------------------
# EventBus tests
# ---------------------------------------------------------------------------

class TestEventBus:
    """Test async event bus."""

    @pytest.mark.asyncio
    async def test_global_subscribe_receives_events(self):
        bus = EventBus()
        q = await bus.subscribe()

        event = Event(
            event_type=EventType.CLUSTER_STATE_CHANGE,
            payload={"cluster_id": "test-1", "state": "up"},
        )
        await bus.publish(event)

        received = await asyncio.wait_for(q.get(), timeout=1.0)
        assert received.event_type == EventType.CLUSTER_STATE_CHANGE
        assert received.payload["cluster_id"] == "test-1"

    @pytest.mark.asyncio
    async def test_topic_subscribe(self):
        bus = EventBus()
        q = await bus.subscribe(topic="cluster-abc")

        event = Event(
            event_type=EventType.LOG_LINE,
            payload={"line": "hello"},
            topic="cluster-abc",
        )
        await bus.publish(event)

        received = await asyncio.wait_for(q.get(), timeout=1.0)
        assert received.payload["line"] == "hello"

    @pytest.mark.asyncio
    async def test_topic_isolation(self):
        bus = EventBus()
        q1 = await bus.subscribe(topic="cluster-a")
        q2 = await bus.subscribe(topic="cluster-b")

        event = Event(
            event_type=EventType.COST_UPDATE,
            payload={"cost": 1.5},
            topic="cluster-a",
        )
        await bus.publish(event)

        # q1 should have the event
        received = await asyncio.wait_for(q1.get(), timeout=1.0)
        assert received.payload["cost"] == 1.5

        # q2 should be empty
        assert q2.empty()

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = EventBus()
        q = await bus.subscribe()
        await bus.unsubscribe(q)

        event = Event(event_type=EventType.ERROR, payload={"msg": "test"})
        await bus.publish(event)

        assert q.empty()


# ---------------------------------------------------------------------------
# Event tests
# ---------------------------------------------------------------------------

class TestEvent:
    """Test Event dataclass."""

    def test_to_json(self):
        event = Event(
            event_type=EventType.CLUSTER_STATE_CHANGE,
            payload={"cluster_id": "c-123", "new_state": "up"},
        )
        json_str = event.to_json()
        assert "cluster_state_change" in json_str
        assert "c-123" in json_str


# ---------------------------------------------------------------------------
# ClusterController state machine tests
# ---------------------------------------------------------------------------

class TestClusterController:
    """Test cluster state machine transitions."""

    @pytest.fixture
    def state_manager(self, tmp_path):
        return StateManager(db_path=str(tmp_path / "test_api_state.db"))

    @pytest.mark.asyncio
    async def test_valid_transitions(self, state_manager):
        bus = EventBus()
        controller = ClusterController(state_manager, bus)

        # We need a VM in state for update_status to work
        state_manager.add_vm({
            "vm_id": "test-cluster",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "pending",
            "provider": "mock",
            "task_name": "test",
        })

        cluster = ClusterRecord(
            cluster_id="test-cluster",
            name="test",
            state=ClusterState.INIT,
            provider="mock",
        )
        controller._clusters[cluster.cluster_id] = cluster

        # INIT → LAUNCHING
        await controller._transition_state(cluster, ClusterState.LAUNCHING)
        assert cluster.state == ClusterState.LAUNCHING

        # LAUNCHING → UP
        await controller._transition_state(cluster, ClusterState.UP)
        assert cluster.state == ClusterState.UP

        # UP → STOPPING
        await controller._transition_state(cluster, ClusterState.STOPPING)
        assert cluster.state == ClusterState.STOPPING

        # STOPPING → STOPPED
        await controller._transition_state(cluster, ClusterState.STOPPED)
        assert cluster.state == ClusterState.STOPPED

        # STOPPED → TERMINATING
        await controller._transition_state(cluster, ClusterState.TERMINATING)
        assert cluster.state == ClusterState.TERMINATING

        # TERMINATING → TERMINATED
        await controller._transition_state(cluster, ClusterState.TERMINATED)
        assert cluster.state == ClusterState.TERMINATED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, state_manager):
        bus = EventBus()
        controller = ClusterController(state_manager, bus)

        cluster = ClusterRecord(
            cluster_id="test-invalid",
            name="test",
            state=ClusterState.INIT,
            provider="mock",
        )

        with pytest.raises(ValueError, match="Invalid state transition"):
            await controller._transition_state(cluster, ClusterState.UP)

    @pytest.mark.asyncio
    async def test_error_from_any_state(self, state_manager):
        bus = EventBus()
        controller = ClusterController(state_manager, bus)

        for initial_state in [ClusterState.INIT, ClusterState.LAUNCHING, ClusterState.UP]:
            vm_id = f"test-error-{initial_state.value}"
            state_manager.add_vm({
                "vm_id": vm_id,
                "ip_address": "127.0.0.1",
                "ssh_port": 22,
                "ssh_user": "root",
                "status": "running",
                "provider": "mock",
                "task_name": "test",
            })

            cluster = ClusterRecord(
                cluster_id=vm_id,
                name="test",
                state=initial_state,
                provider="mock",
            )
            controller._clusters[vm_id] = cluster
            await controller._transition_state(cluster, ClusterState.ERROR)
            assert cluster.state == ClusterState.ERROR

    @pytest.mark.asyncio
    async def test_terminated_is_terminal(self, state_manager):
        bus = EventBus()
        controller = ClusterController(state_manager, bus)

        cluster = ClusterRecord(
            cluster_id="test-terminal",
            name="test",
            state=ClusterState.TERMINATED,
            provider="mock",
        )

        with pytest.raises(ValueError):
            await controller._transition_state(cluster, ClusterState.LAUNCHING)

    @pytest.mark.asyncio
    async def test_transition_emits_event(self, state_manager):
        bus = EventBus()
        q = await bus.subscribe()
        controller = ClusterController(state_manager, bus)

        state_manager.add_vm({
            "vm_id": "test-event",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "pending",
            "provider": "mock",
            "task_name": "test",
        })

        cluster = ClusterRecord(
            cluster_id="test-event",
            name="test",
            state=ClusterState.INIT,
            provider="mock",
        )
        controller._clusters["test-event"] = cluster

        await controller._transition_state(cluster, ClusterState.LAUNCHING)

        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event.event_type == EventType.CLUSTER_STATE_CHANGE


# ---------------------------------------------------------------------------
# ProviderRegistry tests
# ---------------------------------------------------------------------------

class TestProviderRegistryAPI:
    """Test provider registry in API core."""

    def test_mock_provider_registered(self):
        provider = ProviderRegistry.get("mock")
        assert provider is not None

    def test_unknown_provider(self):
        with pytest.raises(ValueError):
            ProviderRegistry.get("nonexistent_provider")

    def test_list_providers(self):
        providers = ProviderRegistry.list_providers()
        assert "mock" in providers
