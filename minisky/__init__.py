"""
MiniSky - Lightweight cloud orchestration tool.

A simplified version of SkyPilot for launching and managing
cloud GPU instances with a simple YAML-based interface.
"""

__version__ = "0.2.0"
__author__ = "MiniSky Team"

from .task import Task, ResourceRequirements, FileMount
from .state import StateManager
from .config import MiniSkyConfig

__all__ = [
    "Task",
    "ResourceRequirements",
    "FileMount",
    "StateManager",
    "MiniSkyConfig",
]
