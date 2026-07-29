"""
MiniSky SDK Models
"""

from typing import Dict, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ClusterState(str, Enum):
    """Cluster state enum."""
    INIT = "init"
    LAUNCHING = "launching"
    UP = "up"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    ERROR = "error"


class JobState(str, Enum):
    """Job state enum."""
    PENDING = "pending"
    SETTING_UP = "setting_up"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_SETUP = "failed_setup"
    CANCELLED = "cancelled"
    RECOVERING = "recovering"


@dataclass
class Cluster:
    """Cluster model."""
    cluster_id: str
    name: str
    state: ClusterState
    provider: str
    num_nodes: int = 1
    head_ip: Optional[str] = None
    instance_type: Optional[str] = None
    accelerators: Optional[Dict[str, int]] = None
    launched_at: Optional[datetime] = None
    autostop_minutes: Optional[int] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Cluster":
        """Create from API response."""
        launched_at = None
        if data.get("launched_at"):
            try:
                launched_at = datetime.fromisoformat(data["launched_at"])
            except (ValueError, TypeError):
                pass
        
        return cls(
            cluster_id=data["cluster_id"],
            name=data["name"],
            state=ClusterState(data["state"]),
            provider=data["provider"],
            num_nodes=data.get("num_nodes", 1),
            head_ip=data.get("head_ip"),
            instance_type=data.get("instance_type"),
            accelerators=data.get("accelerators"),
            launched_at=launched_at,
            autostop_minutes=data.get("autostop_minutes"),
        )
    
    def is_running(self) -> bool:
        """Check if cluster is running."""
        return self.state == ClusterState.UP
    
    def is_terminal(self) -> bool:
        """Check if cluster is in terminal state."""
        return self.state in (ClusterState.TERMINATED, ClusterState.ERROR)


@dataclass
class Job:
    """Job model."""
    job_id: str
    name: str
    state: JobState
    cluster_id: Optional[str] = None
    submitted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    failure_reason: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        """Create from API response."""
        def parse_dt(val: Optional[str]) -> Optional[datetime]:
            if val:
                try:
                    return datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    pass
            return None
        
        return cls(
            job_id=data["job_id"],
            name=data["name"],
            state=JobState(data["state"]),
            cluster_id=data.get("cluster_id"),
            submitted_at=parse_dt(data.get("submitted_at")),
            started_at=parse_dt(data.get("started_at")),
            ended_at=parse_dt(data.get("ended_at")),
            exit_code=data.get("exit_code"),
            failure_reason=data.get("failure_reason"),
        )
    
    def is_running(self) -> bool:
        """Check if job is running."""
        return self.state in (JobState.PENDING, JobState.SETTING_UP, JobState.RUNNING)
    
    def is_terminal(self) -> bool:
        """Check if job is in terminal state."""
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
    
    def succeeded(self) -> bool:
        """Check if job succeeded."""
        return self.state == JobState.SUCCEEDED


@dataclass
class Event:
    """WebSocket event model."""
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime
    topic: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Create from WebSocket message."""
        timestamp = datetime.utcnow()
        if data.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(data["timestamp"])
            except (ValueError, TypeError):
                pass
        
        return cls(
            event_type=data["type"],
            payload=data.get("payload", {}),
            timestamp=timestamp,
            topic=data.get("topic"),
        )
