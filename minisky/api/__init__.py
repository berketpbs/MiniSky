"""
MiniSky API Package

This package provides:
- core.py: Domain model and business logic (state machines, controllers,
  event bus) - no FastAPI dependency, independently usable/testable.
- server.py: FastAPI app (REST endpoints + WebSocket) that delegates to
  core.py's controllers.

Usage:
    # Start API server
    from minisky.api.server import run_server
    run_server(host="0.0.0.0", port=8000)

    # Or use the controllers directly
    from minisky.api import EventBus, ClusterController
    bus = EventBus()
    clusters = ClusterController(bus)
    cluster = await clusters.create_cluster("my-cluster", "mock")
"""

from minisky.api.core import (
    ClusterController,
    JobController,
    ResourceController,
    EventBus,
    Event,
    EventType,
    ClusterState,
    JobState,
    ClusterRecord,
    JobRecord,
    ProviderRegistry,
)

__all__ = [
    "ClusterController",
    "JobController",
    "ResourceController",
    "EventBus",
    "Event",
    "EventType",
    "ClusterState",
    "JobState",
    "ClusterRecord",
    "JobRecord",
    "ProviderRegistry",
]
