"""
MiniSky API Server - Core Module

This is the central orchestration layer that manages:
- Async job lifecycle (submit, monitor, cancel)
- Cluster state machine
- Resource allocation and scheduling
- Real-time event streaming via WebSocket

Architecture follows SkyPilot's controller pattern:
- JobController: Manages job lifecycle and recovery
- ClusterController: Manages VM/cluster state transitions
- ResourceController: Handles resource allocation and optimization

This is NOT a simple CRUD API - it's an orchestration engine.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Set
from enum import Enum
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =============================================================================
# Domain Models (Not DTOs - these represent real domain concepts)
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
# Event System (For real-time updates, not polling)
# =============================================================================

class EventType(str, Enum):
    """Event types for WebSocket streaming."""
    CLUSTER_STATE_CHANGE = "cluster_state_change"
    JOB_STATE_CHANGE = "job_state_change"
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
    
    def to_json(self) -> str:
        return json.dumps({
            "type": self.event_type.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat()
        })


class EventBus:
    """
    In-memory event bus for real-time updates.
    In production, this would be Redis pub/sub or similar.
    """
    
    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._global_subscribers: Set[asyncio.Queue] = set()
    
    async def publish(self, event: Event, topic: Optional[str] = None):
        """Publish event to subscribers."""
        # Global subscribers get all events
        for queue in self._global_subscribers:
            await queue.put(event)
        
        # Topic-specific subscribers
        if topic and topic in self._subscribers:
            for queue in self._subscribers[topic]:
                await queue.put(event)
    
    def subscribe(self, topic: Optional[str] = None) -> asyncio.Queue:
        """Subscribe to events."""
        queue = asyncio.Queue()
        
        if topic:
            if topic not in self._subscribers:
                self._subscribers[topic] = set()
            self._subscribers[topic].add(queue)
        else:
            self._global_subscribers.add(queue)
        
        return queue
    
    def unsubscribe(self, queue: asyncio.Queue, topic: Optional[str] = None):
        """Unsubscribe from events."""
        if topic and topic in self._subscribers:
            self._subscribers[topic].discard(queue)
        else:
            self._global_subscribers.discard(queue)


# =============================================================================
# Controllers (Business Logic Layer)
# =============================================================================

class ClusterController:
    """
    Manages cluster lifecycle state machine.
    
    State transitions:
        INIT -> LAUNCHING -> UP -> STOPPING -> STOPPED -> TERMINATING -> TERMINATED
                         -> ERROR (from any state)
    """
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._clusters: Dict[str, ClusterRecord] = {}
        self._state_locks: Dict[str, asyncio.Lock] = {}
    
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
        
        # Validate transition
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
        
        # Emit event
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
        
        # Actual launch would happen here via provider
        # For now, simulate async launch
        asyncio.create_task(self._do_launch(cluster))
        
        return cluster
    
    async def _do_launch(self, cluster: ClusterRecord):
        """Background task to actually launch cluster."""
        try:
            # Simulate launch time
            await asyncio.sleep(2)
            
            # In real implementation:
            # 1. Call provider.launch()
            # 2. Wait for VM to be ready
            # 3. Setup SSH
            # 4. Run setup commands
            
            cluster.head_ip = "10.0.0.1"  # Would come from provider
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
        """Background task to stop cluster."""
        try:
            await asyncio.sleep(1)
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.STOPPED)
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
        """Background task to terminate cluster."""
        try:
            await asyncio.sleep(1)
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.TERMINATED)
            
            # Clean up
            del self._clusters[cluster.cluster_id]
            del self._state_locks[cluster.cluster_id]
        except Exception as e:
            async with await self._get_lock(cluster.cluster_id):
                await self._transition_state(cluster, ClusterState.ERROR, reason=str(e))
    
    def get_cluster(self, cluster_id: str) -> Optional[ClusterRecord]:
        """Get cluster by ID."""
        return self._clusters.get(cluster_id)
    
    def list_clusters(self) -> List[ClusterRecord]:
        """List all clusters."""
        return list(self._clusters.values())


class JobController:
    """
    Manages job lifecycle and execution.
    
    Jobs are decoupled from clusters - the controller decides
    which cluster to run a job on based on resource requirements.
    """
    
    def __init__(
        self,
        event_bus: EventBus,
        cluster_controller: ClusterController
    ):
        self.event_bus = event_bus
        self.cluster_controller = cluster_controller
        self._jobs: Dict[str, JobRecord] = {}
        self._job_queues: Dict[str, asyncio.Queue] = {}  # Per-cluster job queues
    
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
        
        If cluster_id is provided, job runs on that cluster.
        Otherwise, controller finds or creates appropriate cluster.
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
        
        # Emit event
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
        
        # Start execution
        asyncio.create_task(self._execute_job(job))
        
        return job
    
    async def _execute_job(self, job: JobRecord):
        """Execute job on cluster."""
        try:
            # Find or wait for cluster
            if job.cluster_id:
                cluster = self.cluster_controller.get_cluster(job.cluster_id)
                if not cluster:
                    raise ValueError(f"Cluster not found: {job.cluster_id}")
                
                # Wait for cluster to be UP
                while cluster.state != ClusterState.UP:
                    if cluster.state in (ClusterState.ERROR, ClusterState.TERMINATED):
                        raise RuntimeError(f"Cluster in invalid state: {cluster.state}")
                    await asyncio.sleep(1)
                    cluster = self.cluster_controller.get_cluster(job.cluster_id)
            
            # Transition to SETTING_UP
            job.state = JobState.SETTING_UP
            job.started_at = datetime.utcnow()
            await self._emit_job_state_change(job, JobState.PENDING, JobState.SETTING_UP)
            
            # Setup phase (install deps, sync files, etc.)
            await asyncio.sleep(1)  # Simulate setup
            
            # Transition to RUNNING
            job.state = JobState.RUNNING
            await self._emit_job_state_change(job, JobState.SETTING_UP, JobState.RUNNING)
            
            # Execute (simulate)
            await asyncio.sleep(3)
            
            # Success
            job.state = JobState.SUCCEEDED
            job.ended_at = datetime.utcnow()
            job.exit_code = 0
            await self._emit_job_state_change(job, JobState.RUNNING, JobState.SUCCEEDED)
            
        except Exception as e:
            job.state = JobState.FAILED
            job.ended_at = datetime.utcnow()
            job.failure_reason = str(e)
            await self._emit_job_state_change(job, job.state, JobState.FAILED)
            
            # Handle recovery if enabled
            if job.spot_recovery and job.restart_count < job.max_restarts:
                await self._recover_job(job)
    
    async def _recover_job(self, job: JobRecord):
        """Attempt to recover a failed job."""
        job.state = JobState.RECOVERING
        job.restart_count += 1
        await self._emit_job_state_change(job, JobState.FAILED, JobState.RECOVERING)
        
        # Wait before retry
        await asyncio.sleep(5)
        
        # Reset and re-execute
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
# API Application
# =============================================================================

# Global instances (would be dependency injected in production)
event_bus = EventBus()
cluster_controller = ClusterController(event_bus)
job_controller = JobController(event_bus, cluster_controller)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    print("MiniSky API Server starting...")
    yield
    # Shutdown
    print("MiniSky API Server shutting down...")


app = FastAPI(
    title="MiniSky API",
    description="Cloud orchestration API for ML workloads",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# API Models (DTOs for API layer)
# =============================================================================

class ClusterCreateRequest(BaseModel):
    name: str
    provider: str = "mock"
    num_nodes: int = 1
    instance_type: Optional[str] = None
    accelerators: Optional[Dict[str, int]] = None
    autostop_minutes: Optional[int] = None


class ClusterResponse(BaseModel):
    cluster_id: str
    name: str
    state: str
    provider: str
    num_nodes: int
    head_ip: Optional[str]
    launched_at: Optional[str]
    
    @classmethod
    def from_record(cls, record: ClusterRecord) -> "ClusterResponse":
        return cls(
            cluster_id=record.cluster_id,
            name=record.name,
            state=record.state.value,
            provider=record.provider,
            num_nodes=record.num_nodes,
            head_ip=record.head_ip,
            launched_at=record.launched_at.isoformat() if record.launched_at else None
        )


class JobSubmitRequest(BaseModel):
    name: str
    task_yaml: str
    entrypoint: str
    cluster_id: Optional[str] = None
    spot_recovery: bool = False
    max_restarts: int = 0


class JobResponse(BaseModel):
    job_id: str
    name: str
    state: str
    cluster_id: Optional[str]
    submitted_at: str
    started_at: Optional[str]
    ended_at: Optional[str]
    exit_code: Optional[int]
    failure_reason: Optional[str]
    
    @classmethod
    def from_record(cls, record: JobRecord) -> "JobResponse":
        return cls(
            job_id=record.job_id,
            name=record.name,
            state=record.state.value,
            cluster_id=record.cluster_id,
            submitted_at=record.submitted_at.isoformat(),
            started_at=record.started_at.isoformat() if record.started_at else None,
            ended_at=record.ended_at.isoformat() if record.ended_at else None,
            exit_code=record.exit_code,
            failure_reason=record.failure_reason
        )


# =============================================================================
# API Endpoints
# =============================================================================

# --- Cluster Endpoints ---

@app.post("/v1/clusters", response_model=ClusterResponse)
async def create_cluster(request: ClusterCreateRequest):
    """Create a new cluster."""
    cluster = await cluster_controller.create_cluster(
        name=request.name,
        provider=request.provider,
        num_nodes=request.num_nodes,
        instance_type=request.instance_type,
        accelerators=request.accelerators,
        autostop_minutes=request.autostop_minutes
    )
    return ClusterResponse.from_record(cluster)


@app.post("/v1/clusters/{cluster_id}/launch", response_model=ClusterResponse)
async def launch_cluster(cluster_id: str):
    """Launch a cluster."""
    try:
        cluster = await cluster_controller.launch_cluster(cluster_id)
        return ClusterResponse.from_record(cluster)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/v1/clusters/{cluster_id}/stop", response_model=ClusterResponse)
async def stop_cluster(cluster_id: str):
    """Stop a running cluster."""
    try:
        cluster = await cluster_controller.stop_cluster(cluster_id)
        return ClusterResponse.from_record(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/clusters/{cluster_id}", response_model=ClusterResponse)
async def terminate_cluster(cluster_id: str):
    """Terminate a cluster."""
    try:
        cluster = await cluster_controller.terminate_cluster(cluster_id)
        return ClusterResponse.from_record(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/clusters", response_model=List[ClusterResponse])
async def list_clusters():
    """List all clusters."""
    clusters = cluster_controller.list_clusters()
    return [ClusterResponse.from_record(c) for c in clusters]


@app.get("/v1/clusters/{cluster_id}", response_model=ClusterResponse)
async def get_cluster(cluster_id: str):
    """Get cluster details."""
    cluster = cluster_controller.get_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return ClusterResponse.from_record(cluster)


# --- Job Endpoints ---

@app.post("/v1/jobs", response_model=JobResponse)
async def submit_job(request: JobSubmitRequest):
    """Submit a new job."""
    job = await job_controller.submit_job(
        name=request.name,
        task_yaml=request.task_yaml,
        entrypoint=request.entrypoint,
        cluster_id=request.cluster_id,
        spot_recovery=request.spot_recovery,
        max_restarts=request.max_restarts
    )
    return JobResponse.from_record(job)


@app.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str):
    """Cancel a job."""
    try:
        job = await job_controller.cancel_job(job_id)
        return JobResponse.from_record(job)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/jobs", response_model=List[JobResponse])
async def list_jobs(cluster_id: Optional[str] = None):
    """List jobs."""
    jobs = job_controller.list_jobs(cluster_id=cluster_id)
    return [JobResponse.from_record(j) for j in jobs]


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Get job details."""
    job = job_controller.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.from_record(job)


# --- WebSocket for Real-time Updates ---

@app.websocket("/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time event streaming.
    
    Clients can subscribe to specific topics:
    - cluster:<cluster_id> - Events for specific cluster
    - job:<job_id> - Events for specific job
    - * - All events
    """
    await websocket.accept()
    
    # Subscribe to all events by default
    queue = event_bus.subscribe()
    
    try:
        while True:
            # Wait for events
            event = await queue.get()
            await websocket.send_text(event.to_json())
    except WebSocketDisconnect:
        event_bus.unsubscribe(queue)


# --- Health Check ---

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "clusters": len(cluster_controller.list_clusters()),
        "jobs": len(job_controller.list_jobs())
    }


# =============================================================================
# Entry Point
# =============================================================================

def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the API server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
