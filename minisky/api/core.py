"""
MiniSky API Core - Integration Layer

This module bridges the API server with actual MiniSky components:
- StateManager: Persistent cluster/VM state
- Providers: Cloud provider implementations
- JobQueue: Job execution and tracking
- Storage: File mounts and checkpoints
- SSH: Remote execution

Architecture follows SkyPilot's pattern where the API layer
orchestrates lower-level components rather than reimplementing them.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any, Type
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
import json
import logging

# MiniSky core imports
from minisky.state import StateManager
from minisky.providers.base import BaseProvider
from minisky.providers.mock import MockProvider
from minisky.queue import JobQueue, Job, JobStatus
from minisky.storage import StorageManager, FileMount, MountMode
from minisky.ssh import SSHManager
from minisky.cluster import ClusterManager, NodeRole
from minisky.managed_jobs import ManagedJobController

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# Unified State Enums (Mapping between API and internal states)
# =============================================================================

class ClusterState(str, Enum):
    """Unified cluster state machine."""
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
        """Map VM status to cluster state."""
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
    """Unified job state machine."""
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
        """Map JobQueue status to API job state."""
        mapping = {
            JobStatus.PENDING: cls.PENDING,
            JobStatus.RUNNING: cls.RUNNING,
            JobStatus.COMPLETED: cls.SUCCEEDED,
            JobStatus.FAILED: cls.FAILED,
            JobStatus.CANCELLED: cls.CANCELLED,
        }
        return mapping.get(status, cls.FAILED)


# =============================================================================
# Domain Records (Rich domain objects with behavior)
# =============================================================================

@dataclass
class ClusterRecord:
    """
    Unified cluster record that wraps StateManager VM data.
    
    This is the API's view of a cluster, combining:
    - VM state from StateManager
    - Provider-specific metadata
    - Runtime information (jobs, mounts, etc.)
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
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: Optional[str] = None
    
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
    
    @classmethod
    def from_vm_info(cls, vm_info: Dict[str, Any]) -> "ClusterRecord":
        """Create ClusterRecord from StateManager VM info."""
        task_name = vm_info.get("task_name")
        if task_name is None:
            task_name = vm_info["vm_id"]
        
        created_at = vm_info.get("created_at")
        launched_at = None
        if created_at:
            try:
                launched_at = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                pass
        
        return cls(
            cluster_id=vm_info["vm_id"],
            name=task_name,
            state=ClusterState.from_vm_status(vm_info.get("status", "unknown")),
            provider=vm_info.get("provider", "unknown"),
            head_ip=vm_info.get("ip_address"),
            ssh_user=vm_info.get("ssh_user", "root"),
            ssh_port=vm_info.get("ssh_port", 22),
            ssh_key_path=vm_info.get("ssh_key_path"),
            instance_type=vm_info.get("instance_type"),
            accelerators=vm_info.get("accelerators"),
            launched_at=launched_at,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "cluster_id": self.cluster_id,
            "name": self.name,
            "state": self.state.value,
            "provider": self.provider,
            "region": self.region,
            "num_nodes": self.num_nodes,
            "instance_type": self.instance_type,
            "accelerators": self.accelerators,
            "head_ip": self.head_ip,
            "worker_ips": self.worker_ips,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
            "launched_at": self.launched_at.isoformat() if self.launched_at else None,
            "autostop_minutes": self.autostop_minutes,
            "cost_per_hour": self.cost_per_hour,
            "total_cost": self.total_cost,
        }


@dataclass
class JobRecord:
    """
    Unified job record that wraps JobQueue Job data.
    
    Extends the basic Job with:
    - Recovery configuration
    - Cluster association
    - File mounts
    """
    job_id: str
    name: str
    state: JobState
    
    # Task definition
    command: str
    task_yaml: Optional[str] = None
    
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
    output: Optional[str] = None
    error: Optional[str] = None
    
    @classmethod
    def from_queue_job(cls, job: Job, cluster_id: Optional[str] = None) -> "JobRecord":
        """Create JobRecord from JobQueue Job."""
        job_name = job.job_id
        spot_recovery = False
        max_restarts = 0
        restart_count = 0
        
        if job.metadata:
            job_name = job.metadata.get("name", job.job_id)
            spot_recovery = job.metadata.get("spot_recovery", False)
            max_restarts = job.metadata.get("max_restarts", 0)
            restart_count = job.metadata.get("restart_count", 0)
        
        return cls(
            job_id=job.job_id,
            name=job_name,
            state=JobState.from_job_status(job.status),
            command=job.command,
            cluster_id=cluster_id or job.vm_id,
            submitted_at=datetime.fromtimestamp(job.created_at),
            started_at=datetime.fromtimestamp(job.started_at) if job.started_at else None,
            ended_at=datetime.fromtimestamp(job.completed_at) if job.completed_at else None,
            exit_code=job.exit_code,
            output=job.output,
            error=job.error,
            spot_recovery=spot_recovery,
            max_restarts=max_restarts,
            restart_count=restart_count,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "job_id": self.job_id,
            "name": self.name,
            "state": self.state.value,
            "command": self.command,
            "cluster_id": self.cluster_id,
            "submitted_at": self.submitted_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error,
            "spot_recovery": self.spot_recovery,
            "max_restarts": self.max_restarts,
            "restart_count": self.restart_count,
        }


# =============================================================================
# Event System (Real-time updates via pub/sub)
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
            "topic": self.topic
        })


class EventBus:
    """
    Async event bus for real-time updates.
    
    Supports:
    - Topic-based subscriptions (cluster:xxx, job:xxx)
    - Global subscriptions (all events)
    - Async event delivery
    """
    
    def __init__(self):
        self._subscribers: Dict[str, set] = {}
        self._global_subscribers: set = set()
        self._lock = asyncio.Lock()
    
    async def publish(self, event: Event):
        """Publish event to all relevant subscribers."""
        async with self._lock:
            # Global subscribers
            for queue in list(self._global_subscribers):
                try:
                    await queue.put(event)
                except Exception as e:
                    logger.warning(f"Failed to publish to global subscriber: {e}")
            
            # Topic subscribers
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
                if topic not in self._subscribers:
                    self._subscribers[topic] = set()
                self._subscribers[topic].add(queue)
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
# Provider Registry (Factory pattern for providers)
# =============================================================================

class ProviderRegistry:
    """
    Registry for cloud providers.
    
    Allows dynamic registration and instantiation of providers.
    """
    
    _providers: Dict[str, Type[BaseProvider]] = {
        "mock": MockProvider,
    }
    
    @classmethod
    def register(cls, name: str, provider_class: Type[BaseProvider]):
        """Register a new provider."""
        cls._providers[name] = provider_class
    
    @classmethod
    def get(cls, name: str, config: Optional[Dict[str, Any]] = None) -> BaseProvider:
        """Get provider instance by name."""
        if name not in cls._providers:
            raise ValueError(f"Unknown provider: {name}. Available: {list(cls._providers.keys())}")
        return cls._providers[name](config or {})
    
    @classmethod
    def list_providers(cls) -> List[str]:
        """List available providers."""
        return list(cls._providers.keys())


# =============================================================================
# Cluster Controller (Orchestrates cluster lifecycle)
# =============================================================================

class ClusterController:
    """
    Manages cluster lifecycle by coordinating:
    - StateManager for persistence
    - Providers for cloud operations
    - EventBus for real-time updates
    
    State machine:
        INIT -> LAUNCHING -> UP -> STOPPING -> STOPPED -> TERMINATING -> TERMINATED
                         -> ERROR (from any state)
    """
    
    # Valid state transitions
    VALID_TRANSITIONS: Dict[ClusterState, set] = {
        ClusterState.INIT: {ClusterState.LAUNCHING, ClusterState.ERROR},
        ClusterState.LAUNCHING: {ClusterState.UP, ClusterState.ERROR},
        ClusterState.UP: {ClusterState.STOPPING, ClusterState.TERMINATING, ClusterState.ERROR},
        ClusterState.STOPPING: {ClusterState.STOPPED, ClusterState.ERROR},
        ClusterState.STOPPED: {ClusterState.LAUNCHING, ClusterState.TERMINATING, ClusterState.ERROR},
        ClusterState.TERMINATING: {ClusterState.TERMINATED, ClusterState.ERROR},
        ClusterState.TERMINATED: set(),
        ClusterState.ERROR: {ClusterState.TERMINATING},
    }
    
    def __init__(
        self,
        state_manager: StateManager,
        event_bus: EventBus,
        storage_manager: Optional[StorageManager] = None,
    ):
        self.state = state_manager
        self.event_bus = event_bus
        self.storage = storage_manager
        
        # In-memory cache for active clusters
        self._clusters: Dict[str, ClusterRecord] = {}
        self._providers: Dict[str, BaseProvider] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        
        # Load existing clusters from state
        self._load_existing_clusters()
    
    def _load_existing_clusters(self):
        """Load existing clusters from StateManager."""
        for vm_info in self.state.list_vms():
            cluster = ClusterRecord.from_vm_info(vm_info)
            self._clusters[cluster.cluster_id] = cluster
            logger.info(f"Loaded existing cluster: {cluster.cluster_id} ({cluster.state})")
    
    async def _get_lock(self, cluster_id: str) -> asyncio.Lock:
        """Get or create lock for cluster operations."""
        if cluster_id not in self._locks:
            self._locks[cluster_id] = asyncio.Lock()
        return self._locks[cluster_id]
    
    def _get_provider(self, provider_name: str) -> BaseProvider:
        """Get or create provider instance."""
        if provider_name not in self._providers:
            self._providers[provider_name] = ProviderRegistry.get(provider_name)
        return self._providers[provider_name]
    
    async def _transition_state(
        self,
        cluster: ClusterRecord,
        new_state: ClusterState,
        reason: Optional[str] = None
    ):
        """
        Transition cluster to new state with validation.
        
        This is the ONLY way to change cluster state - ensures
        state machine integrity and event emission.
        """
        old_state = cluster.state
        
        # Validate transition
        valid_next = self.VALID_TRANSITIONS.get(old_state, set())
        if new_state not in valid_next:
            raise ValueError(f"Invalid state transition: {old_state} -> {new_state}")
        
        # Update state
        cluster.state = new_state
        
        # Persist to StateManager
        status_map = {
            ClusterState.INIT: "pending",
            ClusterState.LAUNCHING: "pending",
            ClusterState.UP: "running",
            ClusterState.STOPPING: "stopping",
            ClusterState.STOPPED: "stopped",
            ClusterState.TERMINATING: "terminating",
            ClusterState.TERMINATED: "terminated",
            ClusterState.ERROR: "error",
        }
        self.state.update_status(cluster.cluster_id, status_map[new_state])
        
        # Emit event
        await self.event_bus.publish(Event(
            event_type=EventType.CLUSTER_STATE_CHANGE,
            payload={
                "cluster_id": cluster.cluster_id,
                "name": cluster.name,
                "old_state": old_state.value,
                "new_state": new_state.value,
                "reason": reason,
            },
            topic=f"cluster:{cluster.cluster_id}"
        ))
        
        logger.info(f"Cluster {cluster.cluster_id}: {old_state} -> {new_state}")
    
    async def create_cluster(
        self,
        name: str,
        provider: str = "mock",
        num_nodes: int = 1,
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        autostop_minutes: Optional[int] = None,
        region: Optional[str] = None,
    ) -> ClusterRecord:
        """
        Create a new cluster (does not launch yet).
        
        Returns cluster in INIT state, ready to be launched.
        """
        cluster_id = f"sky-{uuid.uuid4().hex[:8]}"
        
        cluster = ClusterRecord(
            cluster_id=cluster_id,
            name=name,
            state=ClusterState.INIT,
            provider=provider,
            num_nodes=num_nodes,
            instance_type=instance_type,
            accelerators=accelerators,
            autostop_minutes=autostop_minutes,
            region=region,
        )
        
        self._clusters[cluster_id] = cluster
        
        # Emit creation event
        await self.event_bus.publish(Event(
            event_type=EventType.CLUSTER_CREATED,
            payload=cluster.to_dict(),
            topic=f"cluster:{cluster_id}"
        ))
        
        logger.info(f"Created cluster: {cluster_id} ({name})")
        return cluster
    
    async def launch_cluster(self, cluster_id: str) -> ClusterRecord:
        """
        Launch a cluster - starts async provisioning.
        
        Returns immediately with cluster in LAUNCHING state.
        Actual provisioning happens in background task.
        """
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")
        
        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.LAUNCHING)
        
        # Start background launch
        asyncio.create_task(self._do_launch(cluster))
        
        return cluster
    
    async def _do_launch(self, cluster: ClusterRecord):
        """Background task to provision cluster via provider."""
        try:
            provider = self._get_provider(cluster.provider)
            
            # Create task config for provider
            task_config = {
                "name": cluster.name,
                "resources": {
                    "instance_type": cluster.instance_type,
                    "accelerators": cluster.accelerators,
                },
                "num_nodes": cluster.num_nodes,
            }
            
            # Launch via provider
            vm_info = provider.launch(task_config)
            
            # Update cluster with provider response
            cluster.head_ip = vm_info.get("ip_address")
            cluster.ssh_user = vm_info.get("ssh_user", "root")
            cluster.ssh_port = vm_info.get("ssh_port", 22)
            cluster.launched_at = datetime.utcnow()
            
            # Update vm_id to match cluster_id for consistency
            vm_info["vm_id"] = cluster.cluster_id
            
            # Persist to StateManager
            self.state.add_vm(vm_info)
            
            # Transition to UP
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.UP)
            
            # Setup file mounts if configured
            if self.storage and cluster.task_yaml:
                await self._setup_file_mounts(cluster)
                
        except Exception as e:
            logger.error(f"Failed to launch cluster {cluster.cluster_id}: {e}")
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))
    
    async def _setup_file_mounts(self, cluster: ClusterRecord):
        """Setup file mounts on cluster."""
        # Parse task YAML for file_mounts
        # This would integrate with StorageManager
        pass
    
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
        """Background task to stop cluster."""
        try:
            provider = self._get_provider(cluster.provider)
            provider.stop(cluster.cluster_id)
            
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.STOPPED)
                
        except Exception as e:
            logger.error(f"Failed to stop cluster {cluster.cluster_id}: {e}")
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))
    
    async def start_cluster(self, cluster_id: str) -> ClusterRecord:
        """Start a stopped cluster."""
        cluster = self._clusters.get(cluster_id)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_id}")
        
        if cluster.state != ClusterState.STOPPED:
            raise ValueError(f"Can only start stopped clusters, current state: {cluster.state}")
        
        async with await self._get_lock(cluster_id):
            await self._transition_state(cluster, ClusterState.LAUNCHING)
        
        asyncio.create_task(self._do_start(cluster))
        return cluster
    
    async def _do_start(self, cluster: ClusterRecord):
        """Background task to start cluster."""
        try:
            provider = self._get_provider(cluster.provider)
            provider.start(cluster.cluster_id)
            
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.UP)
                
        except Exception as e:
            logger.error(f"Failed to start cluster {cluster.cluster_id}: {e}")
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
        """Background task to terminate cluster."""
        try:
            provider = self._get_provider(cluster.provider)
            provider.terminate(cluster.cluster_id)
            
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.TERMINATED)
            
            # Remove from StateManager
            self.state.remove_vm(cluster.cluster_id)
            
            # Emit deletion event
            await self.event_bus.publish(Event(
                event_type=EventType.CLUSTER_DELETED,
                payload={"cluster_id": cluster.cluster_id},
                topic=f"cluster:{cluster.cluster_id}"
            ))
            
            # Cleanup
            del self._clusters[cluster.cluster_id]
            if cluster.cluster_id in self._locks:
                del self._locks[cluster.cluster_id]
                
        except Exception as e:
            logger.error(f"Failed to terminate cluster {cluster.cluster_id}: {e}")
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
    Manages job lifecycle by coordinating:
    - JobQueue for persistence and tracking
    - ClusterController for execution environment
    - SSHManager for remote execution
    - EventBus for real-time updates
    
    Features:
    - Job submission and cancellation
    - Automatic cluster selection/creation
    - Spot instance recovery
    - Output streaming
    """
    
    def __init__(
        self,
        job_queue: JobQueue,
        cluster_controller: ClusterController,
        event_bus: EventBus,
        ssh_manager: Optional[SSHManager] = None,
    ):
        self.queue = job_queue
        self.clusters = cluster_controller
        self.event_bus = event_bus
        self.ssh = ssh_manager
        
        # In-memory job tracking
        self._jobs: Dict[str, JobRecord] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        
        # Load existing jobs
        self._load_existing_jobs()
    
    def _load_existing_jobs(self):
        """Load pending/running jobs from queue."""
        for job in self.queue.list_jobs():
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                record = JobRecord.from_queue_job(job)
                self._jobs[job.job_id] = record
                logger.info(f"Loaded existing job: {job.job_id} ({record.state})")
    
    async def submit_job(
        self,
        name: str,
        command: str,
        cluster_id: Optional[str] = None,
        task_yaml: Optional[str] = None,
        spot_recovery: bool = False,
        max_restarts: int = 0,
    ) -> JobRecord:
        """
        Submit a job for execution.
        
        If cluster_id is provided, runs on that cluster.
        Otherwise, finds or creates appropriate cluster.
        """
        # Validate cluster if specified
        if cluster_id:
            cluster = self.clusters.get_cluster(cluster_id)
            if not cluster:
                raise ValueError(f"Cluster not found: {cluster_id}")
        
        # Create job in queue
        metadata = {
            "name": name,
            "task_yaml": task_yaml,
            "spot_recovery": spot_recovery,
            "max_restarts": max_restarts,
            "restart_count": 0,
        }
        
        queue_job = self.queue.add_job(
            vm_id=cluster_id or "pending",
            command=command,
            metadata=metadata
        )
        
        # Create job record
        job = JobRecord(
            job_id=queue_job.job_id,
            name=name,
            state=JobState.PENDING,
            command=command,
            task_yaml=task_yaml,
            cluster_id=cluster_id,
            spot_recovery=spot_recovery,
            max_restarts=max_restarts,
        )
        
        self._jobs[job.job_id] = job
        
        # Emit event
        await self.event_bus.publish(Event(
            event_type=EventType.JOB_SUBMITTED,
            payload=job.to_dict(),
            topic=f"job:{job.job_id}"
        ))
        
        # Start execution
        task = asyncio.create_task(self._execute_job(job))
        self._running_tasks[job.job_id] = task
        
        logger.info(f"Submitted job: {job.job_id} ({name})")
        return job
    
    async def _transition_job_state(
        self,
        job: JobRecord,
        new_state: JobState,
        reason: Optional[str] = None
    ):
        """Transition job state and emit event."""
        old_state = job.state
        job.state = new_state
        
        # Update queue
        status_map = {
            JobState.PENDING: JobStatus.PENDING,
            JobState.SETTING_UP: JobStatus.RUNNING,
            JobState.RUNNING: JobStatus.RUNNING,
            JobState.SUCCEEDED: JobStatus.COMPLETED,
            JobState.FAILED: JobStatus.FAILED,
            JobState.FAILED_SETUP: JobStatus.FAILED,
            JobState.CANCELLED: JobStatus.CANCELLED,
            JobState.RECOVERING: JobStatus.PENDING,
        }
        
        if new_state in status_map:
            self.queue.update_status(job.job_id, status_map[new_state])
        
        # Emit event
        await self.event_bus.publish(Event(
            event_type=EventType.JOB_STATE_CHANGE,
            payload={
                "job_id": job.job_id,
                "name": job.name,
                "old_state": old_state.value,
                "new_state": new_state.value,
                "reason": reason,
            },
            topic=f"job:{job.job_id}"
        ))
        
        logger.info(f"Job {job.job_id}: {old_state} -> {new_state}")
    
    async def _execute_job(self, job: JobRecord):
        """Execute job on cluster."""
        try:
            # Wait for cluster to be ready
            if job.cluster_id:
                cluster = self.clusters.get_cluster(job.cluster_id)
                if not cluster:
                    raise ValueError(f"Cluster not found: {job.cluster_id}")
                
                # Wait for cluster to be UP
                max_wait = 300  # 5 minutes
                waited = 0
                while cluster.state not in (ClusterState.UP, ClusterState.ERROR, ClusterState.TERMINATED):
                    if waited >= max_wait:
                        raise TimeoutError(f"Cluster {job.cluster_id} did not become ready in time")
                    await asyncio.sleep(2)
                    waited += 2
                    cluster = self.clusters.get_cluster(job.cluster_id)
                    if not cluster:
                        raise ValueError(f"Cluster disappeared: {job.cluster_id}")
                
                if cluster.state != ClusterState.UP:
                    raise RuntimeError(f"Cluster in invalid state: {cluster.state}")
            
            # Transition to SETTING_UP
            await self._transition_job_state(job, JobState.SETTING_UP)
            job.started_at = datetime.utcnow()
            
            # Setup phase - would install dependencies, sync files, etc.
            await asyncio.sleep(0.5)  # Simulate setup
            
            # Transition to RUNNING
            await self._transition_job_state(job, JobState.RUNNING)
            
            # Execute command
            if self.ssh and job.cluster_id:
                cluster = self.clusters.get_cluster(job.cluster_id)
                if cluster and cluster.head_ip:
                    # Real SSH execution
                    exit_code, output, error = await self._run_ssh_command(
                        cluster, job.command
                    )
                    job.exit_code = exit_code
                    job.output = output
                    job.error = error
                else:
                    # Simulate execution
                    await asyncio.sleep(2)
                    job.exit_code = 0
                    job.output = f"Simulated output for: {job.command}"
            else:
                # Simulate execution
                await asyncio.sleep(2)
                job.exit_code = 0
                job.output = f"Simulated output for: {job.command}"
            
            # Determine final state
            job.ended_at = datetime.utcnow()
            
            if job.exit_code == 0:
                await self._transition_job_state(job, JobState.SUCCEEDED)
                self.queue.mark_completed(job.job_id, job.exit_code or 0, job.output or "")
            else:
                await self._transition_job_state(
                    job, JobState.FAILED,
                    reason=f"Exit code: {job.exit_code}"
                )
                self.queue.mark_failed(job.job_id, job.exit_code or 1, job.error or f"Exit code: {job.exit_code}")
                
                # Handle recovery if enabled
                if job.spot_recovery and job.restart_count < job.max_restarts:
                    await self._recover_job(job)
                    
        except Exception as e:
            logger.error(f"Job {job.job_id} failed: {e}")
            job.ended_at = datetime.utcnow()
            job.error = str(e)
            await self._transition_job_state(job, JobState.FAILED, reason=str(e))
            self.queue.mark_failed(job.job_id, 1, str(e))
            
            # Handle recovery if enabled
            if job.spot_recovery and job.restart_count < job.max_restarts:
                await self._recover_job(job)
        finally:
            # Cleanup running task reference
            if job.job_id in self._running_tasks:
                del self._running_tasks[job.job_id]
    
    async def _run_ssh_command(
        self,
        cluster: ClusterRecord,
        command: str
    ) -> tuple:
        """Run command on cluster via SSH."""
        # This would use SSHManager for real execution
        # For now, simulate
        await asyncio.sleep(1)
        return (0, f"Output of: {command}", "")
    
    async def _recover_job(self, job: JobRecord):
        """Attempt to recover a failed job."""
        job.restart_count += 1
        
        await self._transition_job_state(
            job, JobState.RECOVERING,
            reason=f"Restart attempt {job.restart_count}/{job.max_restarts}"
        )
        
        # Update metadata in queue
        if job.job_id:
            queue_job = self.queue.get_job(job.job_id)
            if queue_job and queue_job.metadata:
                queue_job.metadata["restart_count"] = job.restart_count
        
        # Wait before retry (exponential backoff)
        wait_time = min(30, 5 * (2 ** (job.restart_count - 1)))
        logger.info(f"Job {job.job_id}: waiting {wait_time}s before retry")
        await asyncio.sleep(wait_time)
        
        # Reset state and re-execute
        job.state = JobState.PENDING
        job.error = None
        job.exit_code = None
        job.output = None
        
        task = asyncio.create_task(self._execute_job(job))
        self._running_tasks[job.job_id] = task
    
    async def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a running or pending job."""
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        
        if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            raise ValueError(f"Cannot cancel job in state: {job.state}")
        
        # Cancel running task if exists
        if job_id in self._running_tasks:
            self._running_tasks[job_id].cancel()
            del self._running_tasks[job_id]
        
        job.ended_at = datetime.utcnow()
        await self._transition_job_state(job, JobState.CANCELLED)
        self.queue.cancel_job(job_id)
        
        return job
    
    def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Get job by ID."""
        # Check in-memory first
        if job_id in self._jobs:
            return self._jobs[job_id]
        
        # Check queue
        queue_job = self.queue.get_job(job_id)
        if queue_job:
            return JobRecord.from_queue_job(queue_job)
        
        return None
    
    def list_jobs(
        self,
        cluster_id: Optional[str] = None,
        state: Optional[JobState] = None,
        limit: int = 100
    ) -> List[JobRecord]:
        """List jobs with optional filters."""
        # Get from queue for persistence
        queue_jobs = self.queue.list_jobs(vm_id=cluster_id, limit=limit)
        
        jobs = []
        for qj in queue_jobs:
            record = JobRecord.from_queue_job(qj)
            
            # Apply state filter
            if state and record.state != state:
                continue
            
            # Update from in-memory if available (more current)
            if qj.job_id in self._jobs:
                record = self._jobs[qj.job_id]
            
            jobs.append(record)
        
        return jobs
    
    async def stream_job_output(self, job_id: str) -> asyncio.Queue:
        """
        Stream job output in real-time.
        
        Returns a queue that receives output lines as they're produced.
        """
        queue = await self.event_bus.subscribe(topic=f"job:{job_id}")
        return queue


# =============================================================================
# Resource Controller (Resource allocation and optimization)
# =============================================================================

class ResourceController:
    """
    Manages resource allocation and optimization.
    
    Features:
    - GPU availability tracking
    - Cost estimation
    - Resource recommendations
    - Quota management
    """
    
    def __init__(
        self,
        cluster_controller: ClusterController,
        event_bus: EventBus,
    ):
        self.clusters = cluster_controller
        self.event_bus = event_bus
        
        # GPU pricing (mock data)
        self._gpu_pricing = {
            "V100": 2.48,
            "A100": 3.67,
            "T4": 0.35,
            "A10G": 1.01,
            "H100": 5.50,
        }
    
    def get_available_resources(self) -> Dict[str, Any]:
        """Get summary of available resources across all clusters."""
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
        """Estimate cost for a resource configuration."""
        gpu_cost = 0.0
        
        if accelerators:
            for gpu_type, count in accelerators.items():
                hourly_rate = self._gpu_pricing.get(gpu_type, 1.0)
                gpu_cost += hourly_rate * count * num_nodes * hours
        
        # Base instance cost (simplified)
        instance_cost = 0.5 * num_nodes * hours  # $0.50/hour base
        
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
            ("training", "small"): {
                "accelerators": {"T4": 1},
                "instance_type": "g4dn.xlarge",
                "num_nodes": 1,
            },
            ("training", "medium"): {
                "accelerators": {"A10G": 1},
                "instance_type": "g5.xlarge",
                "num_nodes": 1,
            },
            ("training", "large"): {
                "accelerators": {"A100": 4},
                "instance_type": "p4d.24xlarge",
                "num_nodes": 1,
            },
            ("inference", "small"): {
                "accelerators": {"T4": 1},
                "instance_type": "g4dn.xlarge",
                "num_nodes": 1,
            },
            ("inference", "medium"): {
                "accelerators": {"A10G": 1},
                "instance_type": "g5.xlarge",
                "num_nodes": 1,
            },
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
        
        # Default recommendation
        return {
            "accelerators": {"T4": 1},
            "instance_type": "g4dn.xlarge",
            "num_nodes": 1,
            "estimated_cost_per_hour": 0.85,
        }


# =============================================================================
# API Core Factory (Dependency injection)
# =============================================================================

class APICore:
    """
    Central factory for API components.
    
    Provides dependency injection and lifecycle management
    for all API controllers and services.
    """
    
    def __init__(
        self,
        state_db_path: Optional[str] = None,
        jobs_db_path: Optional[str] = None,
    ):
        # Initialize event bus
        self.event_bus = EventBus()
        
        # Initialize state manager
        self.state_manager = StateManager(db_path=state_db_path)
        
        # Initialize job queue
        self.job_queue = JobQueue(db_path=jobs_db_path)
        
        # Initialize storage manager (optional)
        self.storage_manager: Optional[StorageManager] = None
        try:
            self.storage_manager = StorageManager()
        except Exception as e:
            logger.warning(f"Storage manager not available: {e}")
        
        # SSH manager is created per-cluster, not globally
        # It requires vm_info, so we don't initialize it here
        self.ssh_manager: Optional[SSHManager] = None
        
        # Initialize controllers
        self.cluster_controller = ClusterController(
            state_manager=self.state_manager,
            event_bus=self.event_bus,
            storage_manager=self.storage_manager,
        )
        
        self.job_controller = JobController(
            job_queue=self.job_queue,
            cluster_controller=self.cluster_controller,
            event_bus=self.event_bus,
            ssh_manager=self.ssh_manager,
        )
        
        self.resource_controller = ResourceController(
            cluster_controller=self.cluster_controller,
            event_bus=self.event_bus,
        )
        
        logger.info("APICore initialized")
    
    @classmethod
    def create_default(cls) -> "APICore":
        """Create APICore with default configuration."""
        return cls()
    
    async def shutdown(self):
        """Cleanup resources on shutdown."""
        # Cancel all running job tasks
        for task in self.job_controller._running_tasks.values():
            task.cancel()
        
        logger.info("APICore shutdown complete")


# =============================================================================
# Singleton instance for easy access
# =============================================================================

_api_core: Optional[APICore] = None


def get_api_core() -> APICore:
    """Get or create the global APICore instance."""
    global _api_core
    if _api_core is None:
        _api_core = APICore.create_default()
    return _api_core


def reset_api_core():
    """Reset the global APICore instance (for testing)."""
    global _api_core
    _api_core = None