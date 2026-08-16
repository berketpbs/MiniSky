"""
MiniSky API Core - domain/business-logic layer.

Holds the FastAPI-independent domain model (state machines, records,
event bus) and controllers (ClusterController, JobController,
ResourceController) that minisky/api/server.py's FastAPI routes delegate
to. Kept separate from server.py so this layer can be imported/tested
without pulling in FastAPI, and so it's reusable outside the HTTP API.

Note on scope: ClusterController/JobController persist to StateManager
(cluster/job records survive a server restart), but a job with no
cluster_id still fails clearly rather than auto-provisioning one -
that scheduling logic was never built, and is a separate concern from
persistence. Also, an in-flight background operation (a launch/stop/
terminate/job execution actually running) is lost on restart even
though the record survives - see ClusterController._load_clusters()/
JobController._load_jobs() for how that's handled: anything caught
mid-transition is marked ERROR/FAILED on load rather than resumed,
since there's no way to know what actually happened to it.
"""

import asyncio
import dataclasses
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Type
from enum import Enum
from dataclasses import dataclass, field
import json

import yaml

from minisky.providers import get_provider, register_provider, list_available_providers
from minisky.providers.base import BaseProvider
from minisky.task import Task, ResourceRequirements
from minisky.executor import Executor, ExecutorError
from minisky.queue import JobStatus
from minisky.state import StateManager

logger = logging.getLogger(__name__)


# =============================================================================
# Unified State Enums
# =============================================================================

class ClusterState(str, Enum):
    """Cluster state machine states."""
    INIT = "init"
    LAUNCHING = "launching"
    UP = "up"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    ERROR = "error"

    @classmethod
    def from_vm_status(cls, status: str) -> "ClusterState":
        """Map a provider/VMInfo status string to a ClusterState."""
        mapping = {
            "running": cls.UP,
            "stopped": cls.STOPPED,
            "terminated": cls.TERMINATED,
            "pending": cls.LAUNCHING,
            "stopping": cls.STOPPING,
            "error": cls.ERROR,
        }
        return mapping.get(status, cls.ERROR)


class JobState(str, Enum):
    """Job state machine states."""
    PENDING = "pending"
    SETTING_UP = "setting_up"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_SETUP = "failed_setup"
    CANCELLED = "cancelled"
    RECOVERING = "recovering"

    @classmethod
    def from_job_status(cls, status: JobStatus) -> "JobState":
        """Map a minisky.queue.JobStatus to a JobState."""
        mapping = {
            JobStatus.PENDING: cls.PENDING,
            JobStatus.RUNNING: cls.RUNNING,
            JobStatus.COMPLETED: cls.SUCCEEDED,
            JobStatus.FAILED: cls.FAILED,
            JobStatus.CANCELLED: cls.CANCELLED,
        }
        return mapping.get(status, cls.FAILED)


# =============================================================================
# Domain Records
# =============================================================================

@dataclass
class ClusterRecord:
    """
    Internal cluster record - represents actual cluster state.
    This is the source of truth, not the API response.
    """
    cluster_id: str
    name: str
    state: ClusterState
    provider: str
    region: Optional[str] = None

    # Resources
    num_nodes: int = 1
    instance_type: Optional[str] = None
    accelerators: Optional[Dict[str, int]] = None

    # Network
    head_ip: Optional[str] = None
    worker_ips: List[str] = field(default_factory=list)
    ssh_port: int = 22
    ssh_user: Optional[str] = None

    # Underlying VM identity, as returned by the provider (e.g.
    # "mock-abc123", "runpod-xxx", "aws-i-xxx") - required to call
    # provider.status()/stop()/terminate() on the right instance.
    vm_id: Optional[str] = None

    # Lifecycle
    launched_at: Optional[datetime] = None
    last_use: Optional[datetime] = None
    autostop_minutes: Optional[int] = None

    # Cost tracking
    cost_per_hour: float = 0.0
    total_cost: float = 0.0

    # Metadata
    task_yaml: Optional[str] = None
    user_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRecord:
    """
    Internal job record - represents actual job state.
    Jobs are decoupled from clusters - a job can be retried on different clusters.
    """
    job_id: str
    name: str
    state: JobState

    # Task definition
    task_yaml: str
    entrypoint: str

    # Execution
    cluster_id: Optional[str] = None
    pid: Optional[int] = None

    # Lifecycle
    submitted_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    # Recovery
    spot_recovery: bool = False
    max_restarts: int = 0
    restart_count: int = 0

    # Output
    log_path: Optional[str] = None
    exit_code: Optional[int] = None
    failure_reason: Optional[str] = None


# =============================================================================
# Persistence helpers (ClusterRecord/JobRecord <-> JSON, via StateManager)
# =============================================================================

def _record_to_dict(record: Any) -> Dict[str, Any]:
    """Serialize a ClusterRecord/JobRecord to a JSON-safe dict."""
    d = dataclasses.asdict(record)
    for key, value in d.items():
        if isinstance(value, Enum):
            d[key] = value.value
        elif isinstance(value, datetime):
            d[key] = value.isoformat()
    return d


def _cluster_from_dict(data: Dict[str, Any]) -> ClusterRecord:
    """Deserialize a persisted dict back into a ClusterRecord."""
    d = dict(data)
    d["state"] = ClusterState(d["state"])
    for key in ("launched_at", "last_use"):
        if d.get(key):
            d[key] = datetime.fromisoformat(d[key])
    return ClusterRecord(**d)


def _job_from_dict(data: Dict[str, Any]) -> JobRecord:
    """Deserialize a persisted dict back into a JobRecord."""
    d = dict(data)
    d["state"] = JobState(d["state"])
    for key in ("submitted_at", "started_at", "ended_at"):
        if d.get(key):
            d[key] = datetime.fromisoformat(d[key])
    return JobRecord(**d)


# =============================================================================
# Event System (For real-time updates, not polling)
# =============================================================================

class EventType(str, Enum):
    """Event types for WebSocket streaming."""
    CLUSTER_STATE_CHANGE = "cluster_state_change"
    CLUSTER_CREATED = "cluster_created"
    CLUSTER_DELETED = "cluster_deleted"
    JOB_STATE_CHANGE = "job_state_change"
    JOB_SUBMITTED = "job_submitted"
    JOB_OUTPUT = "job_output"
    LOG_LINE = "log_line"
    RESOURCE_UPDATE = "resource_update"
    COST_UPDATE = "cost_update"
    ERROR = "error"


@dataclass
class Event:
    """Domain event for pub/sub."""
    event_type: EventType
    payload: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    topic: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "type": self.event_type.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "topic": self.topic,
        })


class EventBus:
    """
    Async, thread-safe in-memory event bus for real-time updates.
    In production, this would be Redis pub/sub or similar.

    subscribe()/unsubscribe()/publish() all take the same asyncio.Lock,
    so a subscribe/unsubscribe from one task can never race a concurrent
    publish() iterating the subscriber sets - it simply waits its turn.
    A single bad subscriber queue can't take down delivery to the rest
    either; queue.put() failures are caught and logged per-subscriber.
    """

    def __init__(self):
        self._subscribers: Dict[str, set] = {}
        self._global_subscribers: set = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: Event, topic: Optional[str] = None):
        """
        Publish event to subscribers.

        topic can be set on the Event itself (event.topic=...) or passed
        here - passing it here sets event.topic, so global subscribers
        also see which topic (if any) the event was published under.
        """
        if topic is not None:
            event.topic = topic

        async with self._lock:
            for queue in list(self._global_subscribers):
                try:
                    await queue.put(event)
                except Exception as e:
                    logger.warning(f"Failed to publish to global subscriber: {e}")

            if event.topic and event.topic in self._subscribers:
                for queue in list(self._subscribers[event.topic]):
                    try:
                        await queue.put(event)
                    except Exception as e:
                        logger.warning(f"Failed to publish to topic subscriber: {e}")

    async def subscribe(self, topic: Optional[str] = None) -> asyncio.Queue:
        """Subscribe to events, optionally filtered by topic."""
        queue: asyncio.Queue = asyncio.Queue()

        async with self._lock:
            if topic:
                self._subscribers.setdefault(topic, set()).add(queue)
            else:
                self._global_subscribers.add(queue)

        return queue

    async def unsubscribe(self, queue: asyncio.Queue, topic: Optional[str] = None):
        """Unsubscribe from events."""
        async with self._lock:
            if topic and topic in self._subscribers:
                self._subscribers[topic].discard(queue)
            else:
                self._global_subscribers.discard(queue)


# =============================================================================
# Provider Registry
# =============================================================================

class ProviderRegistry:
    """
    Thin compatibility wrapper around minisky.providers' own registry
    functions (get_provider/register_provider/list_available_providers),
    which already do lazy-loaded provider lookup. Kept as a class purely
    for API stability - avoid duplicating the registry logic itself.
    """

    @classmethod
    def register(cls, name: str, provider_class: Type[BaseProvider]):
        register_provider(name, provider_class)

    @classmethod
    def get(cls, name: str, config: Optional[Dict[str, Any]] = None) -> BaseProvider:
        return get_provider(name, config)

    @classmethod
    def list_providers(cls) -> List[str]:
        return list_available_providers()


# =============================================================================
# Cluster Controller (Orchestrates cluster lifecycle)
# =============================================================================

class ClusterController:
    """
    Manages cluster lifecycle state machine, calling the real cloud
    provider (via get_provider()) to actually launch/stop/terminate VMs.

    State transitions:
        INIT -> LAUNCHING -> UP -> STOPPING -> STOPPED -> TERMINATING -> TERMINATED
                         -> ERROR (from any state)
    """

    # Cluster states that mean "a background task is actively driving this
    # forward right now". Nothing in memory survives a restart, so if a
    # cluster is loaded from disk in one of these, that background task is
    # definitely gone and the cluster's real status is unknown - it's
    # marked ERROR rather than left looking like work is still happening.
    _IN_FLIGHT_STATES = (ClusterState.LAUNCHING, ClusterState.STOPPING, ClusterState.TERMINATING)

    def __init__(self, event_bus: EventBus, state: Optional[StateManager] = None):
        self.event_bus = event_bus
        self.state = state or StateManager()
        self._clusters: Dict[str, ClusterRecord] = {}
        self._state_locks: Dict[str, asyncio.Lock] = {}
        self._load_clusters()

    def _load_clusters(self):
        """Load persisted clusters on startup."""
        for data in self.state.list_cluster_data():
            try:
                cluster = _cluster_from_dict(data)
            except (KeyError, ValueError) as e:
                logger.warning(f"Skipping unreadable persisted cluster record: {e}")
                continue

            if cluster.state in self._IN_FLIGHT_STATES:
                cluster.state = ClusterState.ERROR
                self._persist_cluster(cluster)
                logger.warning(
                    f"Cluster {cluster.cluster_id} was {data['state']} when the server "
                    f"last stopped - marked ERROR, its real status is unknown."
                )

            self._clusters[cluster.cluster_id] = cluster

    def _persist_cluster(self, cluster: ClusterRecord):
        self.state.save_cluster(cluster.cluster_id, _record_to_dict(cluster))

    async def _get_lock(self, cluster_id: str) -> asyncio.Lock:
        """Get or create lock for cluster."""
        if cluster_id not in self._state_locks:
            self._state_locks[cluster_id] = asyncio.Lock()
        return self._state_locks[cluster_id]

    async def _transition_state(
        self,
        cluster: ClusterRecord,
        new_state: ClusterState,
        reason: Optional[str] = None
    ):
        """
        Transition cluster to new state with validation.
        This is the ONLY way to change cluster state.
        """
        old_state = cluster.state

        valid_transitions = {
            ClusterState.INIT: {ClusterState.LAUNCHING, ClusterState.ERROR},
            ClusterState.LAUNCHING: {ClusterState.UP, ClusterState.ERROR},
            ClusterState.UP: {ClusterState.STOPPING, ClusterState.TERMINATING, ClusterState.ERROR},
            ClusterState.STOPPING: {ClusterState.STOPPED, ClusterState.ERROR},
            ClusterState.STOPPED: {ClusterState.LAUNCHING, ClusterState.TERMINATING, ClusterState.ERROR},
            ClusterState.TERMINATING: {ClusterState.TERMINATED, ClusterState.ERROR},
            ClusterState.TERMINATED: set(),  # Terminal state
            ClusterState.ERROR: {ClusterState.TERMINATING},  # Can only terminate from error
        }

        if new_state not in valid_transitions.get(old_state, set()):
            raise ValueError(f"Invalid state transition: {old_state} -> {new_state}")

        cluster.state = new_state
        self._persist_cluster(cluster)

        await self.event_bus.publish(
            Event(
                event_type=EventType.CLUSTER_STATE_CHANGE,
                payload={
                    "cluster_id": cluster.cluster_id,
                    "old_state": old_state.value,
                    "new_state": new_state.value,
                    "reason": reason
                }
            ),
            topic=f"cluster:{cluster.cluster_id}"
        )

    async def create_cluster(
        self,
        name: str,
        provider: str,
        num_nodes: int = 1,
        **kwargs
    ) -> ClusterRecord:
        """Create a new cluster record."""
        cluster_id = f"sky-{uuid.uuid4().hex[:8]}"

        cluster = ClusterRecord(
            cluster_id=cluster_id,
            name=name,
            state=ClusterState.INIT,
            provider=provider,
            num_nodes=num_nodes,
            **kwargs
        )

        self._clusters[cluster_id] = cluster
        self._persist_cluster(cluster)
        return cluster

    async def launch_cluster(self, cluster_id: str) -> ClusterRecord:
        """
        Launch a cluster - this is an async operation.
        Returns immediately, actual launch happens in background.
        """
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")

        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.LAUNCHING)

        asyncio.create_task(self._do_launch(cluster))

        return cluster

    @staticmethod
    def _build_launch_task(cluster: ClusterRecord) -> Task:
        """Build a minimal Task to hand to provider.launch() for a bare
        cluster provision. Setup/run commands aren't part of cluster
        launch - those are supplied separately when a job is submitted."""
        gpu_name, gpu_count = None, 1
        if cluster.accelerators:
            gpu_name, gpu_count = next(iter(cluster.accelerators.items()))

        return Task(
            name=cluster.name,
            provider=cluster.provider,
            resources=ResourceRequirements(gpu=gpu_name, gpu_count=gpu_count),
            run=["true"],
        )

    async def _do_launch(self, cluster: ClusterRecord):
        """Background task to actually launch the cluster's VM via its provider."""
        try:
            provider = get_provider(cluster.provider)
            task = self._build_launch_task(cluster)
            vm_info = await asyncio.to_thread(provider.launch, task)

            cluster.vm_id = vm_info["vm_id"]
            cluster.head_ip = vm_info["ip_address"]
            cluster.ssh_port = vm_info.get("ssh_port", 22)
            cluster.ssh_user = vm_info.get("ssh_user")
            cluster.instance_type = vm_info.get("instance_type", cluster.instance_type)
            cluster.launched_at = datetime.utcnow()

            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.UP)

        except Exception as e:
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(
                    cluster,
                    ClusterState.ERROR,
                    reason=str(e)
                )

    async def stop_cluster(self, cluster_id: str) -> ClusterRecord:
        """Stop a running cluster (preserves disk)."""
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")

        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.STOPPING)

        asyncio.create_task(self._do_stop(cluster))
        return cluster

    async def _do_stop(self, cluster: ClusterRecord):
        """Background task to stop the cluster's VM via its provider."""
        try:
            if cluster.vm_id:
                provider = get_provider(cluster.provider)
                await asyncio.to_thread(provider.stop, cluster.vm_id)
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.STOPPED)
        except Exception as e:
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))

    async def start_cluster(self, cluster_id: str) -> ClusterRecord:
        """Start a previously stopped cluster."""
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")

        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.LAUNCHING)

        asyncio.create_task(self._do_start(cluster))
        return cluster

    async def _do_start(self, cluster: ClusterRecord):
        """Background task to start a stopped cluster's VM via its provider."""
        try:
            provider = get_provider(cluster.provider)
            if cluster.vm_id:
                await asyncio.to_thread(provider.start, cluster.vm_id)

            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.UP)
        except Exception as e:
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))

    async def terminate_cluster(self, cluster_id: str) -> ClusterRecord:
        """Terminate a cluster (destroys all resources)."""
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")

        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.TERMINATING)

        asyncio.create_task(self._do_terminate(cluster))
        return cluster

    async def _do_terminate(self, cluster: ClusterRecord):
        """Background task to terminate the cluster's VM via its provider."""
        try:
            if cluster.vm_id:
                provider = get_provider(cluster.provider)
                await asyncio.to_thread(provider.terminate, cluster.vm_id)
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.TERMINATED)

            await self.event_bus.publish(Event(
                event_type=EventType.CLUSTER_DELETED,
                payload={"cluster_id": cluster.cluster_id},
                topic=f"cluster:{cluster.cluster_id}",
            ))

            # Clean up
            del self._clusters[cluster.cluster_id]
            del self._state_locks[cluster.cluster_id]
            self.state.delete_cluster(cluster.cluster_id)
        except Exception as e:
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))

    def get_cluster(self, cluster_id: str) -> Optional[ClusterRecord]:
        """Get cluster by ID."""
        return self._clusters.get(cluster_id)

    def list_clusters(self, state: Optional[ClusterState] = None) -> List[ClusterRecord]:
        """List clusters, optionally filtered by state."""
        clusters = list(self._clusters.values())
        if state:
            clusters = [c for c in clusters if c.state == state]
        return clusters


# =============================================================================
# Job Controller (Orchestrates job execution)
# =============================================================================

class JobController:
    """
    Manages job lifecycle and execution.

    Jobs are decoupled from clusters - a job runs on whichever cluster_id
    it's submitted with. Executes commands over real SSH via Executor,
    forwarding output live as LOG_LINE events.
    """

    # Job states meaning "a background task is actively driving this
    # forward right now" - see ClusterController._IN_FLIGHT_STATES for why
    # these get corrected on load rather than trusted as-is.
    _IN_FLIGHT_STATES = (JobState.SETTING_UP, JobState.RUNNING, JobState.RECOVERING)

    def __init__(
        self,
        event_bus: EventBus,
        cluster_controller: ClusterController,
        state: Optional[StateManager] = None,
    ):
        self.event_bus = event_bus
        self.cluster_controller = cluster_controller
        self.state = state or StateManager()
        self._jobs: Dict[str, JobRecord] = {}
        self._load_jobs()

    def _load_jobs(self):
        """Load persisted jobs on startup."""
        for data in self.state.list_job_data():
            try:
                job = _job_from_dict(data)
            except (KeyError, ValueError) as e:
                logger.warning(f"Skipping unreadable persisted job record: {e}")
                continue

            if job.state in self._IN_FLIGHT_STATES:
                job.state = JobState.FAILED
                job.failure_reason = (
                    f"Job was {data['state']} when the server last stopped - its real "
                    f"outcome is unknown, since whatever was executing it is gone."
                )
                job.ended_at = datetime.utcnow()
                self._persist_job(job)

            self._jobs[job.job_id] = job

    def _persist_job(self, job: JobRecord):
        self.state.save_job(job.job_id, _record_to_dict(job))

    async def submit_job(
        self,
        name: str,
        task_yaml: str,
        entrypoint: str,
        cluster_id: Optional[str] = None,
        spot_recovery: bool = False,
        max_restarts: int = 0
    ) -> JobRecord:
        """
        Submit a job for execution.

        cluster_id is required - MiniSky doesn't auto-provision/select a
        cluster for a job yet, so one must already exist and be launched.
        """
        job_id = f"job-{uuid.uuid4().hex[:8]}"

        job = JobRecord(
            job_id=job_id,
            name=name,
            state=JobState.PENDING,
            task_yaml=task_yaml,
            entrypoint=entrypoint,
            cluster_id=cluster_id,
            spot_recovery=spot_recovery,
            max_restarts=max_restarts
        )

        self._jobs[job_id] = job
        self._persist_job(job)

        await self.event_bus.publish(
            Event(
                event_type=EventType.JOB_STATE_CHANGE,
                payload={
                    "job_id": job_id,
                    "old_state": None,
                    "new_state": JobState.PENDING.value
                }
            ),
            topic=f"job:{job_id}"
        )

        asyncio.create_task(self._execute_job(job))

        return job

    @staticmethod
    def _build_job_task(job: JobRecord, cluster: ClusterRecord) -> Task:
        """
        Build the Task Executor.execute_task() actually runs.

        `entrypoint` is the authoritative run command (this is what every
        real caller - the SDK's run_training_job() helper included - sets;
        task_yaml is commonly left ""). If task_yaml does parse as a YAML
        mapping, it can supply supplementary fields (setup, env, workdir)
        but never overrides provider/name/run.
        """
        extra: Dict[str, Any] = {}
        if job.task_yaml and job.task_yaml.strip():
            try:
                loaded = yaml.safe_load(job.task_yaml)
                if isinstance(loaded, dict):
                    extra = loaded
            except yaml.YAMLError:
                pass
        extra.pop("name", None)
        extra.pop("provider", None)
        extra.pop("run", None)

        return Task(
            name=job.name,
            provider=cluster.provider,
            run=[job.entrypoint],
            **extra,
        )

    def _make_log_line_forwarder(self, job_id: str) -> Callable[[str, str], None]:
        """
        Build the on_line callback passed to Executor.execute_task().

        Executor runs in a worker thread (via asyncio.to_thread), so this
        callback fires from that thread, not the event loop - publishing
        directly with `await` isn't an option there. Capture the running
        loop up front and hand the publish coroutine to it via
        run_coroutine_threadsafe, fire-and-forget (a slow/backed-up event
        loop shouldn't stall command output from being read).
        """
        loop = asyncio.get_running_loop()

        def on_line(line: str, stream: str):
            event = Event(
                event_type=EventType.LOG_LINE,
                payload={"job_id": job_id, "line": line, "stream": stream},
            )
            asyncio.run_coroutine_threadsafe(
                self.event_bus.publish(event, topic=f"job:{job_id}"),
                loop,
            )

        return on_line

    async def _execute_job(self, job: JobRecord):
        """Execute job on cluster via real SSH (Executor.execute_task)."""
        try:
            if not job.cluster_id:
                # A missing cluster_id can't be scheduled anywhere -
                # auto-provisioning isn't implemented, so fail clearly
                # instead of silently succeeding at running nothing.
                raise ValueError(
                    "cluster_id is required - MiniSky doesn't auto-provision "
                    "a cluster for a job yet. Create and launch one first."
                )

            cluster = self.cluster_controller.get_cluster(job.cluster_id)
            if not cluster:
                raise ValueError(f"Cluster not found: {job.cluster_id}")

            # Wait for cluster to be UP
            while cluster.state != ClusterState.UP:
                if cluster.state in (ClusterState.ERROR, ClusterState.TERMINATED):
                    raise RuntimeError(f"Cluster in invalid state: {cluster.state}")
                await asyncio.sleep(1)
                cluster = self.cluster_controller.get_cluster(job.cluster_id)
                if cluster is None:
                    raise RuntimeError(f"Cluster no longer exists: {job.cluster_id}")

            task = self._build_job_task(job, cluster)
            vm_info = {
                "ip_address": cluster.head_ip,
                "ssh_port": cluster.ssh_port,
                "ssh_user": cluster.ssh_user or "root",
            }

            # Transition to SETTING_UP
            job.state = JobState.SETTING_UP
            job.started_at = datetime.utcnow()
            await self._emit_job_state_change(job, JobState.PENDING, JobState.SETTING_UP)

            # Executor.execute_task() connects, syncs workdir, runs setup
            # then run commands - all blocking (paramiko), so it runs in a
            # worker thread rather than stalling the event loop. It doesn't
            # expose a setup-vs-run boundary callback, so RUNNING is emitted
            # right before handing off rather than mid-execution.
            job.state = JobState.RUNNING
            await self._emit_job_state_change(job, JobState.SETTING_UP, JobState.RUNNING)

            executor = Executor(vm_info)
            on_line = self._make_log_line_forwarder(job.job_id)
            try:
                await asyncio.to_thread(executor.execute_task, task, on_line=on_line)
            except ExecutorError as e:
                raise RuntimeError(str(e)) from e

            # Success
            job.state = JobState.SUCCEEDED
            job.ended_at = datetime.utcnow()
            job.exit_code = 0
            await self._emit_job_state_change(job, JobState.RUNNING, JobState.SUCCEEDED)

        except Exception as e:
            previous_state = job.state
            job.state = JobState.FAILED
            job.ended_at = datetime.utcnow()
            job.failure_reason = str(e)
            await self._emit_job_state_change(job, previous_state, JobState.FAILED)

            if job.spot_recovery and job.restart_count < job.max_restarts:
                await self._recover_job(job)

    async def _recover_job(self, job: JobRecord):
        """Attempt to recover a failed job."""
        job.state = JobState.RECOVERING
        job.restart_count += 1
        await self._emit_job_state_change(job, JobState.FAILED, JobState.RECOVERING)

        await asyncio.sleep(5)

        job.state = JobState.PENDING
        job.failure_reason = None
        asyncio.create_task(self._execute_job(job))

    async def _emit_job_state_change(
        self,
        job: JobRecord,
        old_state: JobState,
        new_state: JobState
    ):
        """Emit job state change event."""
        self._persist_job(job)
        await self.event_bus.publish(
            Event(
                event_type=EventType.JOB_STATE_CHANGE,
                payload={
                    "job_id": job.job_id,
                    "old_state": old_state.value if old_state else None,
                    "new_state": new_state.value
                }
            ),
            topic=f"job:{job.job_id}"
        )

    async def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a running or pending job."""
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            raise ValueError(f"Cannot cancel job in state: {job.state}")

        old_state = job.state
        job.state = JobState.CANCELLED
        job.ended_at = datetime.utcnow()

        await self._emit_job_state_change(job, old_state, JobState.CANCELLED)

        return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Get job by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self, cluster_id: Optional[str] = None) -> List[JobRecord]:
        """List jobs, optionally filtered by cluster."""
        jobs = list(self._jobs.values())
        if cluster_id:
            jobs = [j for j in jobs if j.cluster_id == cluster_id]
        return jobs


# =============================================================================
# Resource Controller (planning/estimation helper - no provider calls)
# =============================================================================

class ResourceController:
    """
    Resource allocation and cost-estimation helpers.

    Features:
    - GPU availability summary across active clusters
    - Cost estimation (static approximate pricing, not a live quote)
    - Resource recommendations by task type/model size
    """

    def __init__(
        self,
        cluster_controller: ClusterController,
        event_bus: EventBus,
    ):
        self.clusters = cluster_controller
        self.event_bus = event_bus

        # Static approximate on-demand pricing (USD/hr) for estimation
        # only - not a live quote.
        self._gpu_pricing = {
            "V100": 2.48,
            "A100": 3.67,
            "T4": 0.35,
            "A10G": 1.01,
            "H100": 5.50,
        }

    def get_available_resources(self) -> Dict[str, Any]:
        """Get summary of available resources across all UP clusters."""
        clusters = self.clusters.list_clusters(state=ClusterState.UP)

        total_gpus: Dict[str, int] = {}
        total_nodes = 0

        for cluster in clusters:
            total_nodes += cluster.num_nodes
            if cluster.accelerators:
                for gpu_type, count in cluster.accelerators.items():
                    total_gpus[gpu_type] = total_gpus.get(gpu_type, 0) + count

        return {
            "total_clusters": len(clusters),
            "total_nodes": total_nodes,
            "gpus": total_gpus,
        }

    def estimate_cost(
        self,
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        num_nodes: int = 1,
        hours: float = 1.0,
    ) -> Dict[str, float]:
        """Estimate cost for a resource configuration (static pricing)."""
        gpu_cost = 0.0

        if accelerators:
            for gpu_type, count in accelerators.items():
                hourly_rate = self._gpu_pricing.get(gpu_type, 1.0)
                gpu_cost += hourly_rate * count * num_nodes * hours

        instance_cost = 0.5 * num_nodes * hours  # $0.50/hour base, simplified

        total = gpu_cost + instance_cost

        return {
            "gpu_cost": gpu_cost,
            "instance_cost": instance_cost,
            "total": total,
            "per_hour": total / hours if hours > 0 else 0,
        }

    def recommend_resources(
        self,
        task_type: str = "training",
        model_size: str = "medium",
    ) -> Dict[str, Any]:
        """Recommend resources based on task requirements."""
        recommendations: Dict[tuple, Dict[str, Any]] = {
            ("training", "small"): {"accelerators": {"T4": 1}, "instance_type": "g4dn.xlarge", "num_nodes": 1},
            ("training", "medium"): {"accelerators": {"A10G": 1}, "instance_type": "g5.xlarge", "num_nodes": 1},
            ("training", "large"): {"accelerators": {"A100": 4}, "instance_type": "p4d.24xlarge", "num_nodes": 1},
            ("inference", "small"): {"accelerators": {"T4": 1}, "instance_type": "g4dn.xlarge", "num_nodes": 1},
            ("inference", "medium"): {"accelerators": {"A10G": 1}, "instance_type": "g5.xlarge", "num_nodes": 1},
        }

        key = (task_type, model_size)
        if key in recommendations:
            rec = recommendations[key]
            accel = rec.get("accelerators")
            nodes = rec.get("num_nodes", 1)
            cost = self.estimate_cost(
                accelerators=accel if isinstance(accel, dict) else None,
                num_nodes=nodes if isinstance(nodes, int) else 1,
                hours=1.0
            )
            result: Dict[str, Any] = dict(rec)
            result["estimated_cost_per_hour"] = cost["per_hour"]
            return result

        return {
            "accelerators": {"T4": 1},
            "instance_type": "g4dn.xlarge",
            "num_nodes": 1,
            "estimated_cost_per_hour": 0.85,
        }
