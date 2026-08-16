"""
MiniSky API Server - FastAPI HTTP/WebSocket layer.

This module is intentionally thin: the domain model and business logic
(state machines, ClusterController, JobController, EventBus, ...) live in
minisky/api/core.py, which has no FastAPI dependency and is independently
testable/reusable. This module only defines the FastAPI app, request/
response DTOs, and route handlers that delegate to those controllers.
"""

from typing import Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from minisky.api.core import (
    ClusterRecord,
    JobRecord,
    EventBus,
    ClusterController,
    JobController,
)


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
    # No cookie/Authorization-based auth exists yet, so there's nothing
    # that needs allow_credentials - and the CORS spec forbids combining
    # it with a wildcard origin anyway (browsers reject the response).
    allow_origins=["*"],
    allow_credentials=False,
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
    instance_type: Optional[str] = None
    accelerators: Optional[Dict[str, int]] = None
    autostop_minutes: Optional[int] = None

    @classmethod
    def from_record(cls, record: ClusterRecord) -> "ClusterResponse":
        return cls(
            cluster_id=record.cluster_id,
            name=record.name,
            state=record.state.value,
            provider=record.provider,
            num_nodes=record.num_nodes,
            head_ip=record.head_ip,
            launched_at=record.launched_at.isoformat() if record.launched_at else None,
            instance_type=record.instance_type,
            accelerators=record.accelerators,
            autostop_minutes=record.autostop_minutes,
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
    queue = await event_bus.subscribe()
    
    try:
        while True:
            # Wait for events
            event = await queue.get()
            await websocket.send_text(event.to_json())
    except WebSocketDisconnect:
        pass
    finally:
        # Always unsubscribe, not just on a clean WebSocketDisconnect -
        # otherwise an abrupt close (e.g. ConnectionResetError) leaks the
        # queue forever and it keeps accumulating every future event.
        await event_bus.unsubscribe(queue)


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
