"""
Task definition and YAML parser for MiniSky.

This module defines the structure of a task and provides
functionality to parse YAML task files. Supports advanced
features like file mounts, spot instances, multi-node,
and port forwarding configuration.
"""

from typing import List, Dict, Optional
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml


class ResourceRequirements(BaseModel):
    """Resource requirements for a VM instance."""

    gpu: Optional[str] = Field(
        None,
        description="GPU type (e.g., 'A100', 'RTX4090', 'V100', 'H100')"
    )
    gpu_count: int = Field(
        1,
        ge=1,
        description="Number of GPUs required"
    )
    cpus: Optional[int] = Field(
        None,
        ge=1,
        description="Number of vCPUs required"
    )
    memory_gb: Optional[int] = Field(
        None,
        ge=1,
        description="RAM in gigabytes"
    )
    disk_gb: int = Field(
        50,
        ge=10,
        description="Disk space in gigabytes"
    )
    use_spot: bool = Field(
        False,
        description="Request spot/preemptible instance for cost savings"
    )
    image_id: Optional[str] = Field(
        None,
        description="Specific OS or Docker image ID to use"
    )

    @field_validator('gpu')
    @classmethod
    def validate_gpu(cls, v: Optional[str]) -> Optional[str]:
        """Validate GPU type format."""
        if v is not None:
            v = v.upper()
        return v


class FileMount(BaseModel):
    """
    File mount specification for syncing data to/from a VM.

    Supports two modes:
    - COPY: Sync files to VM disk before task starts
    - MOUNT: Mount a cloud bucket as a filesystem (future)
    """

    source: str = Field(
        ...,
        description="Source path (local directory or cloud bucket URI)"
    )
    mode: str = Field(
        "COPY",
        description="Mount mode: COPY or MOUNT"
    )

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate mount mode."""
        v = v.upper()
        if v not in ('COPY', 'MOUNT'):
            raise ValueError(f"Invalid mount mode '{v}'. Must be 'COPY' or 'MOUNT'")
        return v


class Task(BaseModel):
    """
    A task represents a unit of work to be executed on a cloud VM.

    Example:
        task = Task(
            name="train-model",
            provider="mock",
            resources=ResourceRequirements(gpu="A100", use_spot=True),
            run=["python train.py"],
            num_nodes=1,
        )
    """

    name: str = Field(
        ...,
        description="Unique name for this task"
    )
    provider: str = Field(
        "mock",
        description="Cloud provider to use (mock, runpod, lambda, aws)"
    )
    resources: ResourceRequirements = Field(
        default_factory=ResourceRequirements,
        description="Resource requirements"
    )
    workdir: Optional[str] = Field(
        None,
        description="Local directory to sync to remote VM"
    )
    file_mounts: Optional[Dict[str, FileMount]] = Field(
        None,
        description="File mount mappings: {remote_path: FileMount}"
    )
    num_nodes: int = Field(
        1,
        ge=1,
        description="Number of nodes for distributed tasks"
    )
    setup: Optional[List[str]] = Field(
        None,
        description="Commands to run during setup phase"
    )
    run: List[str] = Field(
        ...,
        min_length=1,
        description="Commands to execute (main task)"
    )
    env: Optional[Dict[str, str]] = Field(
        None,
        description="Environment variables"
    )
    ports: Optional[List[int]] = Field(
        None,
        description="Ports to expose/forward (e.g., 8080 for Jupyter)"
    )
    autostop_minutes: Optional[int] = Field(
        None,
        ge=1,
        description="Auto-stop VM after this many idle minutes"
    )

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        """Validate provider is supported."""
        supported = ['mock', 'runpod', 'lambda', 'aws']
        if v.lower() not in supported:
            raise ValueError(
                f"Provider '{v}' not supported. "
                f"Supported providers: {', '.join(supported)}"
            )
        return v.lower()

    @field_validator('workdir')
    @classmethod
    def validate_workdir(cls, v: Optional[str]) -> Optional[str]:
        """Validate workdir exists if specified."""
        if v is not None:
            path = Path(v).expanduser()
            if not path.exists():
                raise ValueError(f"Workdir does not exist: {v}")
            if not path.is_dir():
                raise ValueError(f"Workdir is not a directory: {v}")
        return v

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Task":
        """
        Load task from YAML file.

        Args:
            yaml_path: Path to YAML file

        Returns:
            Task instance

        Raises:
            FileNotFoundError: If YAML file doesn't exist
            ValueError: If YAML is invalid
        """
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Task file not found: {yaml_path}")

        with open(path, 'r') as f:
            data = yaml.safe_load(f)

        if data is None:
            raise ValueError(f"Empty YAML file: {yaml_path}")

        # Handle file_mounts shorthand: if value is a string, treat as source
        if 'file_mounts' in data and isinstance(data['file_mounts'], dict):
            processed = {}
            for remote_path, mount_spec in data['file_mounts'].items():
                if isinstance(mount_spec, str):
                    processed[remote_path] = {'source': mount_spec, 'mode': 'COPY'}
                elif isinstance(mount_spec, dict):
                    processed[remote_path] = mount_spec
                else:
                    raise ValueError(
                        f"Invalid file_mount value for '{remote_path}': {mount_spec}"
                    )
            data['file_mounts'] = processed

        return cls(**data)

    def to_yaml(self, yaml_path: str) -> None:
        """
        Save task to YAML file.

        Args:
            yaml_path: Path to save YAML file
        """
        with open(yaml_path, 'w') as f:
            yaml.dump(self.model_dump(exclude_none=True), f, default_flow_style=False)
