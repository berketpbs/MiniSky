"""
Task definition and YAML parser for MiniSky.

This module defines the structure of a task and provides
functionality to parse YAML task files.
"""

from typing import List, Dict, Optional
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml


class ResourceRequirements(BaseModel):
    """Resource requirements for a VM instance."""
    
    gpu: Optional[str] = Field(
        None,
        description="GPU type (e.g., 'A100', 'RTX4090', 'V100')"
    )
    gpu_count: int = Field(
        1,
        ge=1,
        description="Number of GPUs required"
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
    
    @field_validator('gpu')
    @classmethod
    def validate_gpu(cls, v: Optional[str]) -> Optional[str]:
        """Validate GPU type format."""
        if v is not None:
            v = v.upper()
        return v


class Task(BaseModel):
    """
    A task represents a unit of work to be executed on a cloud VM.
    
    Example:
        task = Task(
            name="train-model",
            provider="mock",
            resources=ResourceRequirements(gpu="A100"),
            run=["python train.py"]
        )
    """
    
    name: str = Field(
        ...,
        description="Unique name for this task"
    )
    provider: str = Field(
        "mock",
        description="Cloud provider to use (mock, runpod, lambda)"
    )
    resources: ResourceRequirements = Field(
        default_factory=ResourceRequirements,
        description="Resource requirements"
    )
    workdir: Optional[str] = Field(
        None,
        description="Local directory to sync to remote VM"
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
    
    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        """Validate provider is supported."""
        supported = ['mock', 'runpod', 'lambda']
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
        
        return cls(**data)
    
    def to_yaml(self, yaml_path: str) -> None:
        """
        Save task to YAML file.
        
        Args:
            yaml_path: Path to save YAML file
        """
        with open(yaml_path, 'w') as f:
            yaml.dump(self.model_dump(exclude_none=True), f, default_flow_style=False)
