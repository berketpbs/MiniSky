"""
MiniSky - Lightweight cloud orchestration tool.

A simplified version of SkyPilot for launching and managing
cloud GPU instances with a simple YAML-based interface.
"""

__version__ = "0.1.0"
__author__ = "MiniSky Team"

from .task import Task, ResourceRequirements
from .state import StateManager

__all__ = [
    "Task",
    "ResourceRequirements",
    "StateManager",
]
