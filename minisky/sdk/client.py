"""
MiniSky SDK Client

Sync and async clients for MiniSky API.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Any, AsyncIterator
import logging

from minisky.sdk.models import Cluster, Job, Event, ClusterState, JobState
from minisky.sdk.exceptions import MiniSkyError, APIError, TimeoutError

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

logger = logging.getLogger(__name__)


# =============================================================================
# Sync Client
# =============================================================================

class ClusterAPI:
    """Cluster operations API."""
    
    def __init__(self, client: "MiniSkyClient"):
        self._client = client
    
    def create(
        self,
        name: str,
        provider: str = "mock",
        num_nodes: int = 1,
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        autostop_minutes: Optional[int] = None,
    ) -> Cluster:
        """Create a new cluster."""
        data: Dict[str, Any] = {
            "name": name,
            "provider": provider,
            "num_nodes": num_nodes,
        }
        if instance_type:
            data["instance_type"] = instance_type
        if accelerators:
            data["accelerators"] = accelerators
        if autostop_minutes is not None:
            data["autostop_minutes"] = autostop_minutes
        
        response = self._client._post("/v1/clusters", data)
        return Cluster.from_dict(response)
    
    def launch(self, cluster_id: str) -> Cluster:
        """Launch a cluster."""
        response = self._client._post(f"/v1/clusters/{cluster_id}/launch")
        return Cluster.from_dict(response)
    
    def stop(self, cluster_id: str) -> Cluster:
        """Stop a cluster."""
        response = self._client._post(f"/v1/clusters/{cluster_id}/stop")
        return Cluster.from_dict(response)
    
    def terminate(self, cluster_id: str) -> Cluster:
        """Terminate a cluster."""
        response = self._client._delete(f"/v1/clusters/{cluster_id}")
        return Cluster.from_dict(response)
    
    def get(self, cluster_id: str) -> Cluster:
        """Get cluster details."""
        response = self._client._get(f"/v1/clusters/{cluster_id}")
        return Cluster.from_dict(response)
    
    def list(self) -> List[Cluster]:
        """List all clusters."""
        response = self._client._get("/v1/clusters")
        return [Cluster.from_dict(c) for c in response]
    
    def wait_until_ready(
        self,
        cluster_id: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Cluster:
        """Wait until cluster is ready (UP state)."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            cluster = self.get(cluster_id)
            
            if cluster.state == ClusterState.UP:
                return cluster
            
            if cluster.is_terminal():
                raise MiniSkyError(f"Cluster entered terminal state: {cluster.state}")
            
            time.sleep(poll_interval)
        
        raise TimeoutError(f"Cluster did not become ready within {timeout}s")


class JobAPI:
    """Job operations API."""
    
    def __init__(self, client: "MiniSkyClient"):
        self._client = client
    
    def submit(
        self,
        name: str,
        task_yaml: str,
        entrypoint: str,
        cluster_id: Optional[str] = None,
        spot_recovery: bool = False,
        max_restarts: int = 0,
    ) -> Job:
        """Submit a new job."""
        data: Dict[str, Any] = {
            "name": name,
            "task_yaml": task_yaml,
            "entrypoint": entrypoint,
            "spot_recovery": spot_recovery,
            "max_restarts": max_restarts,
        }
        if cluster_id:
            data["cluster_id"] = cluster_id
        
        response = self._client._post("/v1/jobs", data)
        return Job.from_dict(response)
    
    def cancel(self, job_id: str) -> Job:
        """Cancel a job."""
        response = self._client._post(f"/v1/jobs/{job_id}/cancel")
        return Job.from_dict(response)
    
    def get(self, job_id: str) -> Job:
        """Get job details."""
        response = self._client._get(f"/v1/jobs/{job_id}")
        return Job.from_dict(response)
    
    def list(self, cluster_id: Optional[str] = None) -> List[Job]:
        """List jobs."""
        params = {}
        if cluster_id:
            params["cluster_id"] = cluster_id
        
        response = self._client._get("/v1/jobs", params=params)
        return [Job.from_dict(j) for j in response]
    
    def wait_until_complete(
        self,
        job_id: str,
        timeout: int = 3600,
        poll_interval: int = 10,
    ) -> Job:
        """Wait until job completes."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            job = self.get(job_id)
            
            if job.is_terminal():
                return job
            
            time.sleep(poll_interval)
        
        raise TimeoutError(f"Job did not complete within {timeout}s")


class MiniSkyClient:
    """
    Synchronous MiniSky API client.
    
    Usage:
        client = MiniSkyClient("http://localhost:8000")
        
        # Create and launch cluster
        cluster = client.clusters.create("my-cluster")
        cluster = client.clusters.launch(cluster.cluster_id)
        cluster = client.clusters.wait_until_ready(cluster.cluster_id)
        
        # Submit job
        job = client.jobs.submit(
            name="train",
            task_yaml="...",
            entrypoint="python train.py",
            cluster_id=cluster.cluster_id
        )
        job = client.jobs.wait_until_complete(job.job_id)
        
        # Cleanup
        client.clusters.terminate(cluster.cluster_id)
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        if httpx is None:
            raise ImportError("httpx is required for MiniSkyClient. Install with: pip install httpx")
        
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        
        self._client = httpx.Client(timeout=timeout)
        
        # API namespaces
        self.clusters = ClusterAPI(self)
        self.jobs = JobAPI(self)
    
    def _get_headers(self) -> Dict[str, str]:
        """Get request headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    def _handle_response(self, response: Any) -> Any:
        """Handle API response."""
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise APIError(response.status_code, "Request failed", detail)
        
        return response.json()
    
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Make GET request."""
        url = f"{self.base_url}{path}"
        response = self._client.get(url, headers=self._get_headers(), params=params)
        return self._handle_response(response)
    
    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Any:
        """Make POST request."""
        url = f"{self.base_url}{path}"
        response = self._client.post(url, headers=self._get_headers(), json=data or {})
        return self._handle_response(response)
    
    def _delete(self, path: str) -> Any:
        """Make DELETE request."""
        url = f"{self.base_url}{path}"
        response = self._client.delete(url, headers=self._get_headers())
        return self._handle_response(response)
    
    def health(self) -> Dict[str, Any]:
        """Check API health."""
        return self._get("/health")
    
    def close(self):
        """Close the client."""
        self._client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


# =============================================================================
# Async Client
# =============================================================================

class AsyncClusterAPI:
    """Async cluster operations API."""
    
    def __init__(self, client: "AsyncMiniSkyClient"):
        self._client = client
    
    async def create(
        self,
        name: str,
        provider: str = "mock",
        num_nodes: int = 1,
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        autostop_minutes: Optional[int] = None,
    ) -> Cluster:
        """Create a new cluster."""
        data: Dict[str, Any] = {
            "name": name,
            "provider": provider,
            "num_nodes": num_nodes,
        }
        if instance_type:
            data["instance_type"] = instance_type
        if accelerators:
            data["accelerators"] = accelerators
        if autostop_minutes is not None:
            data["autostop_minutes"] = autostop_minutes
        
        response = await self._client._post("/v1/clusters", data)
        return Cluster.from_dict(response)
    
    async def launch(self, cluster_id: str) -> Cluster:
        """Launch a cluster."""
        response = await self._client._post(f"/v1/clusters/{cluster_id}/launch")
        return Cluster.from_dict(response)
    
    async def stop(self, cluster_id: str) -> Cluster:
        """Stop a cluster."""
        response = await self._client._post(f"/v1/clusters/{cluster_id}/stop")
        return Cluster.from_dict(response)
    
    async def terminate(self, cluster_id: str) -> Cluster:
        """Terminate a cluster."""
        response = await self._client._delete(f"/v1/clusters/{cluster_id}")
        return Cluster.from_dict(response)
    
    async def get(self, cluster_id: str) -> Cluster:
        """Get cluster details."""
        response = await self._client._get(f"/v1/clusters/{cluster_id}")
        return Cluster.from_dict(response)
    
    async def list(self) -> List[Cluster]:
        """List all clusters."""
        response = await self._client._get("/v1/clusters")
        return [Cluster.from_dict(c) for c in response]
    
    async def wait_until_ready(
        self,
        cluster_id: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Cluster:
        """Wait until cluster is ready (UP state)."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            cluster = await self.get(cluster_id)
            
            if cluster.state == ClusterState.UP:
                return cluster
            
            if cluster.is_terminal():
                raise MiniSkyError(f"Cluster entered terminal state: {cluster.state}")
            
            await asyncio.sleep(poll_interval)
        
        raise TimeoutError(f"Cluster did not become ready within {timeout}s")


class AsyncJobAPI:
    """Async job operations API."""
    
    def __init__(self, client: "AsyncMiniSkyClient"):
        self._client = client
    
    async def submit(
        self,
        name: str,
        task_yaml: str,
        entrypoint: str,
        cluster_id: Optional[str] = None,
        spot_recovery: bool = False,
        max_restarts: int = 0,
    ) -> Job:
        """Submit a new job."""
        data: Dict[str, Any] = {
            "name": name,
            "task_yaml": task_yaml,
            "entrypoint": entrypoint,
            "spot_recovery": spot_recovery,
            "max_restarts": max_restarts,
        }
        if cluster_id:
            data["cluster_id"] = cluster_id
        
        response = await self._client._post("/v1/jobs", data)
        return Job.from_dict(response)
    
    async def cancel(self, job_id: str) -> Job:
        """Cancel a job."""
        response = await self._client._post(f"/v1/jobs/{job_id}/cancel")
        return Job.from_dict(response)
    
    async def get(self, job_id: str) -> Job:
        """Get job details."""
        response = await self._client._get(f"/v1/jobs/{job_id}")
        return Job.from_dict(response)
    
    async def list(self, cluster_id: Optional[str] = None) -> List[Job]:
        """List jobs."""
        params = {}
        if cluster_id:
            params["cluster_id"] = cluster_id
        
        response = await self._client._get("/v1/jobs", params=params)
        return [Job.from_dict(j) for j in response]
    
    async def wait_until_complete(
        self,
        job_id: str,
        timeout: int = 3600,
        poll_interval: int = 10,
    ) -> Job:
        """Wait until job completes."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            job = await self.get(job_id)
            
            if job.is_terminal():
                return job
            
            await asyncio.sleep(poll_interval)
        
        raise TimeoutError(f"Job did not complete within {timeout}s")


class AsyncMiniSkyClient:
    """
    Asynchronous MiniSky API client.
    
    Usage:
        async with AsyncMiniSkyClient("http://localhost:8000") as client:
            # Create and launch cluster
            cluster = await client.clusters.create("my-cluster")
            cluster = await client.clusters.launch(cluster.cluster_id)
            cluster = await client.clusters.wait_until_ready(cluster.cluster_id)
            
            # Subscribe to events
            async for event in client.subscribe():
                print(f"Event: {event.event_type}")
            
            # Cleanup
            await client.clusters.terminate(cluster.cluster_id)
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        if httpx is None:
            raise ImportError("httpx is required. Install with: pip install httpx")
        
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        
        self._client: Optional[Any] = None
        self._client_lock = asyncio.Lock()

        # API namespaces
        self.clusters = AsyncClusterAPI(self)
        self.jobs = AsyncJobAPI(self)

    async def _ensure_client(self):
        """Ensure HTTP client is initialized.

        Guarded by a lock: without it, two coroutines can both see
        self._client is None and each construct an httpx.AsyncClient - the
        first one gets overwritten and its connections/sockets leak since
        it's never closed.
        """
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.timeout)
    
    def _get_headers(self) -> Dict[str, str]:
        """Get request headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    async def _handle_response(self, response: Any) -> Any:
        """Handle API response."""
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise APIError(response.status_code, "Request failed", detail)
        
        return response.json()
    
    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Make GET request."""
        await self._ensure_client()
        url = f"{self.base_url}{path}"
        response = await self._client.get(url, headers=self._get_headers(), params=params)
        return await self._handle_response(response)
    
    async def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Any:
        """Make POST request."""
        await self._ensure_client()
        url = f"{self.base_url}{path}"
        response = await self._client.post(url, headers=self._get_headers(), json=data or {})
        return await self._handle_response(response)
    
    async def _delete(self, path: str) -> Any:
        """Make DELETE request."""
        await self._ensure_client()
        url = f"{self.base_url}{path}"
        response = await self._client.delete(url, headers=self._get_headers())
        return await self._handle_response(response)
    
    async def health(self) -> Dict[str, Any]:
        """Check API health."""
        return await self._get("/health")
    
    async def subscribe(
        self,
        topic: Optional[str] = None,
    ) -> AsyncIterator[Event]:
        """
        Subscribe to real-time events via WebSocket.
        
        Args:
            topic: Optional topic filter (e.g., "cluster:xxx", "job:xxx")
        
        Yields:
            Event objects as they arrive
        """
        if websockets is None:
            raise ImportError("websockets is required. Install with: pip install websockets")
        
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/v1/ws"
        
        async with websockets.connect(ws_url) as ws:
            async for message in ws:
                try:
                    data = json.loads(message)
                    event = Event.from_dict(data)
                    
                    # Filter by topic if specified - an event with no topic
                    # at all never matches a requested filter, so it must
                    # be skipped too, not just one with a non-matching topic.
                    if topic and not (event.topic and event.topic.startswith(topic)):
                        continue
                    
                    yield event
                except json.JSONDecodeError:
                    logger.warning(f"Invalid WebSocket message: {message}")
    
    async def close(self):
        """Close the client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def __aenter__(self):
        await self._ensure_client()
        return self
    
    async def __aexit__(self, *args):
        await self.close()


# =============================================================================
# CI/CD Integration Helpers
# =============================================================================

class CIHelper:
    """
    Helper class for CI/CD integration.
    
    Provides simplified methods for common CI/CD workflows.
    """
    
    def __init__(self, client: MiniSkyClient):
        self.client = client
    
    def run_training_job(
        self,
        name: str,
        command: str,
        provider: str = "mock",
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        timeout: int = 3600,
        cleanup: bool = True,
    ) -> Job:
        """
        Run a training job end-to-end.
        
        Creates cluster, runs job, waits for completion, and optionally cleans up.
        """
        cluster = None
        try:
            # Create and launch cluster
            cluster = self.client.clusters.create(
                name=f"{name}-cluster",
                provider=provider,
                instance_type=instance_type,
                accelerators=accelerators,
            )
            cluster = self.client.clusters.launch(cluster.cluster_id)
            cluster = self.client.clusters.wait_until_ready(cluster.cluster_id)
            
            # Submit job
            job = self.client.jobs.submit(
                name=name,
                task_yaml="",
                entrypoint=command,
                cluster_id=cluster.cluster_id,
            )
            
            # Wait for completion
            job = self.client.jobs.wait_until_complete(job.job_id, timeout=timeout)
            
            return job
            
        finally:
            if cleanup and cluster:
                try:
                    self.client.clusters.terminate(cluster.cluster_id)
                except Exception as e:
                    logger.warning(f"Failed to cleanup cluster: {e}")


class AsyncCIHelper:
    """Async version of CI helper."""
    
    def __init__(self, client: AsyncMiniSkyClient):
        self.client = client
    
    async def run_training_job(
        self,
        name: str,
        command: str,
        provider: str = "mock",
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        timeout: int = 3600,
        cleanup: bool = True,
    ) -> Job:
        """Run a training job end-to-end (async)."""
        cluster = None
        try:
            # Create and launch cluster
            cluster = await self.client.clusters.create(
                name=f"{name}-cluster",
                provider=provider,
                instance_type=instance_type,
                accelerators=accelerators,
            )
            cluster = await self.client.clusters.launch(cluster.cluster_id)
            cluster = await self.client.clusters.wait_until_ready(cluster.cluster_id)
            
            # Submit job
            job = await self.client.jobs.submit(
                name=name,
                task_yaml="",
                entrypoint=command,
                cluster_id=cluster.cluster_id,
            )
            
            # Wait for completion
            job = await self.client.jobs.wait_until_complete(job.job_id, timeout=timeout)
            
            return job
            
        finally:
            if cleanup and cluster:
                try:
                    await self.client.clusters.terminate(cluster.cluster_id)
                except Exception as e:
                    logger.warning(f"Failed to cleanup cluster: {e}")
    
    async def run_batch_jobs(
        self,
        jobs: List[Dict[str, Any]],
        provider: str = "mock",
        max_parallel: int = 4,
        timeout: int = 3600,
    ) -> List[Job]:
        """Run multiple jobs in parallel (async)."""
        semaphore = asyncio.Semaphore(max_parallel)
        
        async def run_with_semaphore(job_config: Dict[str, Any]) -> Job:
            async with semaphore:
                return await self.run_training_job(
                    name=job_config["name"],
                    command=job_config["command"],
                    provider=provider,
                    timeout=timeout,
                )
        
        tasks = [run_with_semaphore(jc) for jc in jobs]
        return await asyncio.gather(*tasks)
