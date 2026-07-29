"""
MiniSky Python SDK

A Python client library for interacting with MiniSky API server.

Features:
- Sync and async API clients
- Type-safe request/response models
- Automatic retry and error handling
- WebSocket support for real-time updates
- CI/CD integration helpers

Usage:
    # Sync client
    from minisky.sdk import MiniSkyClient
    
    client = MiniSkyClient("http://localhost:8000")
    cluster = client.clusters.create("my-cluster", provider="mock")
    cluster = client.clusters.launch(cluster.cluster_id)
    
    # Async client
    from minisky.sdk import AsyncMiniSkyClient
    
    async with AsyncMiniSkyClient("http://localhost:8000") as client:
        cluster = await client.clusters.create("my-cluster")
        await client.clusters.launch(cluster.cluster_id)
"""

from minisky.sdk.client import (
    MiniSkyClient,
    AsyncMiniSkyClient,
    ClusterAPI,
    JobAPI,
    AsyncClusterAPI,
    AsyncJobAPI,
    CIHelper,
    AsyncCIHelper,
)

from minisky.sdk.models import (
    Cluster,
    Job,
    Event,
    ClusterState,
    JobState,
)

from minisky.sdk.exceptions import (
    MiniSkyError,
    APIError,
    ConnectionError,
    TimeoutError,
)

__all__ = [
    # Clients
    "MiniSkyClient",
    "AsyncMiniSkyClient",
    # API classes
    "ClusterAPI",
    "JobAPI",
    "AsyncClusterAPI",
    "AsyncJobAPI",
    # CI helpers
    "CIHelper",
    "AsyncCIHelper",
    # Models
    "Cluster",
    "Job",
    "Event",
    "ClusterState",
    "JobState",
    # Exceptions
    "MiniSkyError",
    "APIError",
    "ConnectionError",
    "TimeoutError",
]
