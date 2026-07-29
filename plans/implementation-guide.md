# MiniSky Implementation Guide

## Quick Start Development Order

Follow this exact order to avoid getting stuck:

### Step 1: Project Setup
1. Create directory structure
2. Initialize uv project
3. Install dependencies
4. Verify imports work

### Step 2: Task Parser (Easiest First)
1. Define Pydantic models
2. Implement YAML loading
3. Write validation tests
4. Create example YAML files

### Step 3: Base Provider Interface
1. Define abstract base class
2. Document expected behavior
3. Create type hints

### Step 4: Mock Provider (Critical for Testing)
1. Implement in-memory VM tracking
2. Generate fake IPs and IDs
3. Simulate delays
4. Test without real infrastructure

### Step 5: State Management
1. Create SQLite schema
2. Implement CRUD operations
3. Test persistence
4. Handle edge cases

### Step 6: CLI (Brings it Together)
1. Implement launch command
2. Implement status command
3. Implement terminate command
4. Add rich formatting

### Step 7: Executor (Most Complex)
1. SSH connection logic
2. File sync implementation
3. Command execution
4. Output streaming

### Step 8: Integration Testing
1. End-to-end workflow tests
2. Error handling tests
3. State persistence tests

## Detailed Module Implementation

### 1. Task Parser (`task.py`)

```python
"""
Task definition and YAML parser for MiniSky.

This module defines the structure of a task and provides
functionality to parse YAML task files.
"""

from typing import List, Dict, Optional
from pathlib import Path
from pydantic import BaseModel, Field, validator
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
    
    @validator('gpu')
    def validate_gpu(cls, v):
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
        min_items=1,
        description="Commands to execute (main task)"
    )
    env: Optional[Dict[str, str]] = Field(
        None,
        description="Environment variables"
    )
    
    @validator('provider')
    def validate_provider(cls, v):
        """Validate provider is supported."""
        supported = ['mock', 'runpod', 'lambda']
        if v.lower() not in supported:
            raise ValueError(
                f"Provider '{v}' not supported. "
                f"Supported providers: {', '.join(supported)}"
            )
        return v.lower()
    
    @validator('workdir')
    def validate_workdir(cls, v):
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
            yaml.dump(self.dict(exclude_none=True), f, default_flow_style=False)


# Example usage and testing
if __name__ == "__main__":
    # Test task creation
    task = Task(
        name="test-task",
        provider="mock",
        resources=ResourceRequirements(gpu="A100", gpu_count=1),
        run=["echo 'Hello World'"]
    )
    print(task.json(indent=2))
```

### 2. Base Provider (`providers/base.py`)

```python
"""
Abstract base class for cloud providers.

All provider implementations must inherit from BaseProvider
and implement all abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from ..task import Task


class ProviderError(Exception):
    """Base exception for provider errors."""
    pass


class VMInfo(Dict[str, Any]):
    """
    Type hint for VM information dictionary.
    
    Required keys:
        - vm_id: Unique identifier for the VM
        - ip_address: Public IP address
        - ssh_port: SSH port (usually 22)
        - status: Current status (launching, running, terminated)
        
    Optional keys:
        - ssh_key_path: Path to SSH private key
        - ssh_user: SSH username (default: root)
        - provider: Provider name
        - created_at: Creation timestamp
    """
    pass


class BaseProvider(ABC):
    """
    Abstract base class for cloud providers.
    
    Each provider must implement methods to:
    - Launch new VM instances
    - Check status of instances
    - Terminate instances
    - List all instances
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize provider with configuration.
        
        Args:
            config: Provider-specific configuration (API keys, etc.)
        """
        self.config = config or {}
    
    @abstractmethod
    def launch(self, task: Task) -> VMInfo:
        """
        Launch a new VM instance based on task requirements.
        
        Args:
            task: Task definition with resource requirements
            
        Returns:
            VMInfo dictionary with instance details
            
        Raises:
            ProviderError: If launch fails
        """
        pass
    
    @abstractmethod
    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of a VM instance.
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            VMInfo dictionary with current status
            
        Raises:
            ProviderError: If VM not found or API error
        """
        pass
    
    @abstractmethod
    def terminate(self, vm_id: str) -> bool:
        """
        Terminate a VM instance.
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            True if termination successful
            
        Raises:
            ProviderError: If termination fails
        """
        pass
    
    @abstractmethod
    def list_instances(self) -> List[VMInfo]:
        """
        List all active instances managed by this provider.
        
        Returns:
            List of VMInfo dictionaries
            
        Raises:
            ProviderError: If API error
        """
        pass
    
    def validate_resources(self, task: Task) -> bool:
        """
        Validate that provider can fulfill resource requirements.
        
        Override this method to add provider-specific validation.
        
        Args:
            task: Task with resource requirements
            
        Returns:
            True if resources can be fulfilled
            
        Raises:
            ProviderError: If resources cannot be fulfilled
        """
        return True
```

### 3. Mock Provider (`providers/mock.py`)

```python
"""
Mock cloud provider for testing and development.

This provider simulates cloud operations without making real API calls
or launching actual VMs. Perfect for development and testing.
"""

import time
import uuid
from typing import Dict, List
from .base import BaseProvider, VMInfo, ProviderError
from ..task import Task


class MockProvider(BaseProvider):
    """
    Mock provider that simulates cloud operations.
    
    Features:
    - Generates fake VM IDs and IPs
    - Tracks VMs in memory
    - Simulates API delays
    - No actual infrastructure required
    """
    
    def __init__(self, config: Dict = None):
        super().__init__(config)
        self._instances: Dict[str, VMInfo] = {}
        self._simulate_delay = config.get('simulate_delay', True) if config else True
    
    def _generate_vm_id(self) -> str:
        """Generate a fake VM ID."""
        return f"mock-{uuid.uuid4().hex[:8]}"
    
    def _generate_ip(self) -> str:
        """Generate a fake IP address (localhost for testing)."""
        # Use localhost so we can actually SSH to it for testing
        return "127.0.0.1"
    
    def _simulate_api_delay(self, seconds: float = 0.5):
        """Simulate API response time."""
        if self._simulate_delay:
            time.sleep(seconds)
    
    def launch(self, task: Task) -> VMInfo:
        """
        Simulate launching a VM instance.
        
        Args:
            task: Task definition
            
        Returns:
            VMInfo with fake instance details
        """
        self._simulate_api_delay(1.0)  # Simulate launch time
        
        vm_id = self._generate_vm_id()
        vm_info: VMInfo = {
            'vm_id': vm_id,
            'ip_address': self._generate_ip(),
            'ssh_port': 22,
            'ssh_user': 'root',
            'status': 'running',
            'provider': 'mock',
            'task_name': task.name,
            'resources': task.resources.dict(),
            'created_at': time.time()
        }
        
        self._instances[vm_id] = vm_info
        return vm_info
    
    def status(self, vm_id: str) -> VMInfo:
        """
        Get status of a mock VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            VMInfo with current status
            
        Raises:
            ProviderError: If VM not found
        """
        self._simulate_api_delay(0.2)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        return self._instances[vm_id]
    
    def terminate(self, vm_id: str) -> bool:
        """
        Simulate terminating a VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if successful
            
        Raises:
            ProviderError: If VM not found
        """
        self._simulate_api_delay(0.5)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        self._instances[vm_id]['status'] = 'terminated'
        del self._instances[vm_id]
        return True
    
    def list_instances(self) -> List[VMInfo]:
        """
        List all mock instances.
        
        Returns:
            List of VMInfo dictionaries
        """
        self._simulate_api_delay(0.3)
        return list(self._instances.values())


# Example usage
if __name__ == "__main__":
    from ..task import Task, ResourceRequirements
    
    # Create mock provider
    provider = MockProvider()
    
    # Create a test task
    task = Task(
        name="test-task",
        provider="mock",
        resources=ResourceRequirements(gpu="A100"),
        run=["echo 'test'"]
    )
    
    # Launch VM
    print("Launching VM...")
    vm_info = provider.launch(task)
    print(f"Launched: {vm_info}")
    
    # Check status
    print("\nChecking status...")
    status = provider.status(vm_info['vm_id'])
    print(f"Status: {status}")
    
    # List instances
    print("\nListing instances...")
    instances = provider.list_instances()
    print(f"Active instances: {len(instances)}")
    
    # Terminate
    print("\nTerminating VM...")
    provider.terminate(vm_info['vm_id'])
    print("Terminated successfully")
```

### 4. State Management (`state.py`)

```python
"""
State management for tracking VM instances.

Uses SQLite to persist VM information locally so users can
manage instances across sessions.
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime
from contextlib import contextmanager


class StateManager:
    """
    Manages persistent state of VM instances using SQLite.
    
    Storage location: ~/.minisky/state.db
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize state manager.
        
        Args:
            db_path: Custom database path (default: ~/.minisky/state.db)
        """
        if db_path is None:
            minisky_dir = Path.home() / '.minisky'
            minisky_dir.mkdir(exist_ok=True)
            db_path = str(minisky_dir / 'state.db')
        
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Create database schema if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS vms (
                    vm_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    ssh_port INTEGER DEFAULT 22,
                    ssh_user TEXT DEFAULT 'root',
                    ssh_key_path TEXT,
                    status TEXT NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def add_vm(self, vm_info: Dict[str, Any]) -> None:
        """
        Add a new VM to state tracking.
        
        Args:
            vm_info: VM information dictionary
        """
        with self._get_connection() as conn:
            # Extract metadata (anything not in core fields)
            core_fields = {
                'vm_id', 'provider', 'task_name', 'ip_address',
                'ssh_port', 'ssh_user', 'ssh_key_path', 'status'
            }
            metadata = {k: v for k, v in vm_info.items() if k not in core_fields}
            
            conn.execute('''
                INSERT INTO vms (
                    vm_id, provider, task_name, ip_address,
                    ssh_port, ssh_user, ssh_key_path, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                vm_info['vm_id'],
                vm_info.get('provider', 'unknown'),
                vm_info.get('task_name', 'unnamed'),
                vm_info['ip_address'],
                vm_info.get('ssh_port', 22),
                vm_info.get('ssh_user', 'root'),
                vm_info.get('ssh_key_path'),
                vm_info.get('status', 'unknown'),
                json.dumps(metadata)
            ))
            conn.commit()
    
    def get_vm(self, vm_id: str) -> Optional[Dict[str, Any]]:
        """
        Get VM information by ID.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            VM info dictionary or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'SELECT * FROM vms WHERE vm_id = ?',
                (vm_id,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            vm_info = dict(row)
            # Parse metadata JSON
            if vm_info['metadata']:
                metadata = json.loads(vm_info['metadata'])
                vm_info.update(metadata)
            del vm_info['metadata']
            
            return vm_info
    
    def list_vms(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all VMs, optionally filtered by status.
        
        Args:
            status: Filter by status (e.g., 'running', 'terminated')
            
        Returns:
            List of VM info dictionaries
        """
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    'SELECT * FROM vms WHERE status = ? ORDER BY created_at DESC',
                    (status,)
                )
            else:
                cursor = conn.execute(
                    'SELECT * FROM vms ORDER BY created_at DESC'
                )
            
            vms = []
            for row in cursor.fetchall():
                vm_info = dict(row)
                if vm_info['metadata']:
                    metadata = json.loads(vm_info['metadata'])
                    vm_info.update(metadata)
                del vm_info['metadata']
                vms.append(vm_info)
            
            return vms
    
    def update_status(self, vm_id: str, status: str) -> bool:
        """
        Update VM status.
        
        Args:
            vm_id: VM identifier
            status: New status
            
        Returns:
            True if updated, False if VM not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute('''
                UPDATE vms
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE vm_id = ?
            ''', (status, vm_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def remove_vm(self, vm_id: str) -> bool:
        """
        Remove VM from tracking.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if removed, False if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                'DELETE FROM vms WHERE vm_id = ?',
                (vm_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def cleanup_terminated(self, older_than_days: int = 7) -> int:
        """
        Remove terminated VMs older than specified days.
        
        Args:
            older_than_days: Remove VMs terminated more than this many days ago
            
        Returns:
            Number of VMs removed
        """
        with self._get_connection() as conn:
            cursor = conn.execute('''
                DELETE FROM vms
                WHERE status = 'terminated'
                AND updated_at < datetime('now', '-' || ? || ' days')
            ''', (older_than_days,))
            conn.commit()
            return cursor.rowcount
```

### 5. CLI Interface (`cli.py`)

```python
"""
Command-line interface for MiniSky.

Provides commands for launching, managing, and terminating cloud VMs.
"""

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from pathlib import Path
from typing import Optional

from .task import Task
from .state import StateManager
from .providers import get_provider
from .executor import Executor

app = typer.Typer(
    name="minisky",
    help="Mini SkyPilot - Lightweight cloud orchestration tool",
    add_completion=False
)
console = Console()
state = StateManager()


@app.command()
def launch(
    task_file: str = typer.Argument(..., help="Path to task YAML file"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Launch and detach (don't wait for completion)")
):
    """
    Launch a new task on a cloud VM.
    
    Example:
        minisky launch task.yaml
    """
    try:
        # Parse task
        console.print(f"[cyan]Loading task from {task_file}...[/cyan]")
        task = Task.from_yaml(task_file)
        console.print(f"[green]✓[/green] Task '{task.name}' loaded")
        
        # Get provider
        provider = get_provider(task.provider)
        
        # Launch VM
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Launching VM...", total=None)
            vm_info = provider.launch(task)
        
        console.print(f"[green]✓[/green] VM launched: {vm_info['vm_id']}")
        console.print(f"  IP: {vm_info['ip_address']}")
        console.print(f"  Provider: {vm_info['provider']}")
        
        # Save to state
        state.add_vm(vm_info)
        
        if not detach:
            # Execute task
            console.print("\n[cyan]Executing task...[/cyan]")
            executor = Executor(vm_info)
            executor.execute_task(task)
            console.print("[green]✓[/green] Task completed")
        else:
            console.print("\n[yellow]Task launched in detached mode[/yellow]")
            console.print(f"Use 'minisky logs {vm_info['vm_id']}' to view logs")
        
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


@app.command()
def status(
    vm_id: Optional[str] = typer.Argument(None, help="Specific VM ID to check")
):
    """
    Show status of VMs.
    
    Example:
        minisky status              # Show all VMs
        minisky status mock-abc123  # Show specific VM
    """
    try:
        if vm_id:
            # Show specific VM
            vm_info = state.get_vm(vm_id)
            if not vm_info:
                console.print(f"[red]VM not found:[/red] {vm_id}")
                raise typer.Exit(1)
            
            console.print(f"\n[bold]VM: {vm_id}[/bold]")
            console.print(f"  Status: {vm_info['status']}")
            console.print(f"  IP: {vm_info['ip_address']}")
            console.print(f"  Provider: {vm_info['provider']}")
            console.print(f"  Task: {vm_info['task_name']}")
            console.print(f"  Created: {vm_info['created_at']}")
        else:
            # Show all VMs
            vms = state.list_vms()
            
            if not vms:
                console.print("[yellow]No VMs found[/yellow]")
                return
            
            table = Table(title="Active VMs")
            table.add_column("VM ID", style="cyan")
            table.add_column("Task", style="magenta")
            table.add_column("Provider", style="blue")
            table.add_column("IP Address", style="green")
            table.add_column("Status", style="yellow")
            
            for vm in vms:
                table.add_row(
                    vm['vm_id'],
                    vm['task_name'],
                    vm['provider'],
                    vm['ip_address'],
                    vm['status']
                )
            
            console.print(table)
    
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


@app.command()
def terminate(
    vm_id: str = typer.Argument(..., help="VM ID to terminate"),
    force: bool = typer.Option(False, "--force", "-f", help="Force termination without confirmation")
):
    """
    Terminate a VM instance.
    
    Example:
        minisky terminate mock-abc123
    """
    try:
        # Get VM info
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)
        
        # Confirm
        if not force:
            confirm = typer.confirm(
                f"Terminate VM {vm_id} ({vm_info['task_name']})?"
            )
            if not confirm:
                console.print("[yellow]Cancelled[/yellow]")
                return
        
        # Terminate
        provider = get_provider(vm_info['provider'])
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Terminating VM...", total=None)
            provider.terminate(vm_id)
        
        # Update state
        state.remove_vm(vm_id)
        
        console.print(f"[green]✓[/green] VM terminated: {vm_id}")
    
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


@app.command()
def logs(
    vm_id: str = typer.Argument(..., help="VM ID to view logs"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output")
):
    """
    View logs from a running task.
    
    Example:
        minisky logs mock-abc123
        minisky logs mock-abc123 --follow
    """
    console.print("[yellow]Log streaming not yet implemented[/yellow]")
    # TODO: Implement log streaming


if __name__ == "__main__":
    app()
```

## Testing Strategy

### Test Structure

```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures
├── test_task.py             # Task parsing tests
├── test_providers.py        # Provider tests
├── test_state.py            # State management tests
├── test_executor.py         # Executor tests
└── test_integration.py      # End-to-end tests
```

### Example Test (`tests/test_task.py`)

```python
"""Tests for task parsing and validation."""

import pytest
from pathlib import Path
from minisky.task import Task, ResourceRequirements
from pydantic import ValidationError


def test_task_creation():
    """Test creating a task programmatically."""
    task = Task(
        name="test-task",
        provider="mock",
        resources=ResourceRequirements(gpu="A100"),
        run=["echo 'test'"]
    )
    
    assert task.name == "test-task"
    assert task.provider == "mock"
    assert task.resources.gpu == "A100"
    assert len(task.run) == 1


def test_task_validation_missing_run():
    """Test that task requires run commands."""
    with pytest.raises(ValidationError):
        Task(
            name="test",
            provider="mock",
            run=[]  # Empty run commands should fail
        )


def test_task_from_yaml(tmp_path):
    """Test loading task from YAML file."""
    yaml_content = """
name: test-task
provider: mock
resources:
  gpu: A100
  gpu_count: 2
run:
  - python train.py
"""
    yaml_file = tmp_path / "task.yaml"
    yaml_file.write_text(yaml_content)
    
    task = Task.from_yaml(str(yaml_file))
    
    assert task.name == "test-task"
    assert task.resources.gpu == "A100"
    assert task.resources.gpu_count == 2


def test_invalid_provider():
    """Test that invalid provider raises error."""
    with pytest.raises(ValidationError):
        Task(
            name="test",
            provider="invalid-provider",
            run=["echo 'test'"]
        )
```

## Next Steps

1. Review this implementation guide
2. Confirm the approach and code structure
3. Switch to Code mode to begin implementation
4. Start with Phase 1: Foundation (Mock Provider)

The implementation should follow this order:
1. Project structure setup
2. Task parser (easiest, no dependencies)
3. Base provider interface
4. Mock provider (enables testing)
5. State management
6. CLI (brings everything together)
7. Executor (most complex)
8. Tests throughout

This approach ensures you can test each component as you build it, without needing real cloud infrastructure until the very end.
