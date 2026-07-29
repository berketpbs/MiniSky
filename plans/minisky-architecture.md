# MiniSky - Mini SkyPilot Architecture Plan

## Project Overview

MiniSky is a lightweight cloud orchestration tool inspired by SkyPilot. It allows users to launch, manage, and execute tasks on cloud GPU instances through a simple YAML-based interface.

## Project Structure

```
minisky/
├── minisky/                # Main package directory
│   ├── __init__.py
│   ├── cli.py              # Command-line interface (User interaction)
│   ├── task.py             # Task definition and YAML parser
│   ├── state.py            # Database/State management (Track active VMs)
│   ├── executor.py         # SSH connection, file copying, and command execution
│   └── providers/          # Cloud providers (Each in separate file)
│       ├── __init__.py
│       ├── base.py         # Abstract base class for providers
│       ├── mock.py         # Mock provider for testing
│       └── runpod.py       # (Future) RunPod API integration
├── tests/                  # Unit tests
│   ├── __init__.py
│   ├── test_task.py
│   ├── test_providers.py
│   ├── test_state.py
│   └── test_executor.py
├── examples/               # Example task YAML files
│   ├── simple_task.yaml
│   ├── gpu_training.yaml
│   └── data_processing.yaml
├── pyproject.toml          # Project dependencies (uv)
└── README.md
```

## Core Components

### 1. CLI Module (`cli.py`)

**Purpose**: Entry point for user commands

**Key Features**:
- Command: `minisky launch <task.yaml>` - Launch a new task
- Command: `minisky status` - Show all active VMs
- Command: `minisky terminate <vm_id>` - Terminate a specific VM
- Command: `minisky logs <vm_id>` - Stream logs from a running task

**Technology**: Typer (modern CLI framework with type hints)

**Responsibilities**:
- Parse command-line arguments
- Load and validate task YAML files
- Coordinate between task parser, provider, and executor
- Display formatted output to user (using Rich for colored output)

### 2. Task Module (`task.py`)

**Purpose**: Define task structure and parse YAML configurations

**Key Features**:
- Parse YAML task definitions
- Validate task configuration
- Support for setup and run commands
- File synchronization specifications
- Resource requirements (GPU type, memory, etc.)

**Technology**: Pydantic (data validation), PyYAML (YAML parsing)

**Data Model**:
```python
class ResourceRequirements(BaseModel):
    gpu: Optional[str] = None  # e.g., "A100", "RTX4090"
    gpu_count: int = 1
    memory_gb: Optional[int] = None
    disk_gb: int = 50

class Task(BaseModel):
    name: str
    provider: str  # e.g., "mock", "runpod", "lambda"
    resources: ResourceRequirements
    workdir: Optional[str] = None  # Local directory to sync
    setup: Optional[List[str]] = None  # Setup commands
    run: List[str]  # Main commands to execute
    env: Optional[Dict[str, str]] = None  # Environment variables
```

**Example YAML**:
```yaml
name: train-model
provider: mock
resources:
  gpu: A100
  gpu_count: 1
  memory_gb: 32
workdir: ./my-project
setup:
  - pip install -r requirements.txt
  - python setup.py
run:
  - python train.py --epochs 100
env:
  WANDB_API_KEY: ${WANDB_API_KEY}
```

### 3. Provider Module (`providers/`)

**Purpose**: Abstract cloud provider interactions

#### Base Provider (`base.py`)

**Technology**: ABC (Abstract Base Class)

**Interface**:
```python
from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseProvider(ABC):
    @abstractmethod
    def launch(self, task: Task) -> Dict[str, Any]:
        """
        Launch a new VM instance
        Returns: {
            "vm_id": str,
            "ip_address": str,
            "ssh_port": int,
            "ssh_key_path": str,
            "status": str
        }
        """
        pass
    
    @abstractmethod
    def status(self, vm_id: str) -> Dict[str, Any]:
        """
        Get status of a VM
        Returns: {
            "vm_id": str,
            "status": str,  # "running", "stopped", "terminated"
            "ip_address": str
        }
        """
        pass
    
    @abstractmethod
    def terminate(self, vm_id: str) -> bool:
        """
        Terminate a VM instance
        Returns: True if successful
        """
        pass
    
    @abstractmethod
    def list_instances(self) -> List[Dict[str, Any]]:
        """
        List all active instances
        """
        pass
```

#### Mock Provider (`mock.py`)

**Purpose**: Simulate cloud operations for testing without real API calls

**Features**:
- Simulate VM launch with fake IP addresses (127.0.0.1)
- Track "virtual" VMs in memory
- Simulate delays for realistic behavior
- No actual SSH connection required
- Perfect for development and testing

**Implementation Details**:
- Generate random VM IDs
- Return localhost IP for SSH testing
- Maintain in-memory state of mock VMs
- Simulate API response times

### 4. State Management (`state.py`)

**Purpose**: Track active VMs and their metadata locally

**Technology**: SQLite3 (built-in Python library)

**Database Schema**:
```sql
CREATE TABLE vms (
    vm_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    task_name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    ssh_port INTEGER DEFAULT 22,
    ssh_key_path TEXT,
    status TEXT NOT NULL,  -- 'launching', 'running', 'terminated'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Key Operations**:
- `add_vm(vm_info: Dict)` - Register a new VM
- `get_vm(vm_id: str)` - Retrieve VM information
- `list_vms()` - List all active VMs
- `update_status(vm_id: str, status: str)` - Update VM status
- `remove_vm(vm_id: str)` - Remove VM from tracking

**Storage Location**: `~/.minisky/state.db`

### 5. Executor Module (`executor.py`)

**Purpose**: Execute tasks on remote VMs via SSH

**Technology**: 
- Paramiko or Fabric (SSH connections)
- Rich (terminal output formatting and progress bars)

**Key Features**:
- SSH connection management
- File synchronization (workdir → remote VM)
- Command execution with real-time output streaming
- Environment variable injection
- Error handling and retry logic

**Workflow**:
1. Establish SSH connection to VM
2. Create remote working directory
3. Sync local workdir to remote (using rsync-like approach)
4. Execute setup commands sequentially
5. Execute run commands with stdout/stderr streaming
6. Handle disconnections and errors gracefully

**Implementation Details**:
```python
class Executor:
    def __init__(self, vm_info: Dict):
        self.vm_info = vm_info
        self.ssh_client = None
    
    def connect(self) -> bool:
        """Establish SSH connection"""
        pass
    
    def sync_files(self, local_path: str, remote_path: str):
        """Sync local directory to remote VM"""
        pass
    
    def execute_command(self, command: str, env: Dict = None) -> int:
        """Execute command and stream output"""
        pass
    
    def execute_task(self, task: Task):
        """Execute full task workflow"""
        pass
    
    def disconnect(self):
        """Close SSH connection"""
        pass
```

## Development Phases

### Phase 1: Foundation (Mock Provider Only)
**Goal**: Build core infrastructure without real cloud APIs

**Steps**:
1. Set up project structure
2. Implement Task model and YAML parser
3. Create base provider interface
4. Implement mock provider
5. Build state management
6. Create basic CLI (launch, status, terminate)
7. Write unit tests

**Validation**: Can launch mock VMs, track them, and terminate them

### Phase 2: SSH Executor
**Goal**: Add remote execution capabilities

**Steps**:
1. Implement SSH connection logic
2. Add file synchronization
3. Add command execution with streaming
4. Test with local SSH server or mock
5. Write executor tests

**Validation**: Can connect to localhost and execute commands

### Phase 3: Real Provider Integration
**Goal**: Add first real cloud provider (RunPod)

**Steps**:
1. Study RunPod API documentation
2. Implement RunPod provider
3. Add API key management
4. Test with real RunPod instances
5. Update documentation

**Validation**: Can launch real GPU instances on RunPod

### Phase 4: Polish and Features
**Goal**: Improve user experience

**Steps**:
1. Add better error messages
2. Add progress indicators
3. Add log streaming command
4. Add cost estimation
5. Improve documentation
6. Add more example tasks

## Technology Stack

### Core Dependencies
- **typer**: Modern CLI framework with excellent type support
- **pydantic**: Data validation and settings management
- **pyyaml**: YAML parsing
- **httpx**: Modern async HTTP client for API calls
- **paramiko**: SSH protocol implementation
- **rich**: Beautiful terminal formatting and progress bars
- **pytest**: Testing framework

### Development Dependencies
- **pytest-cov**: Code coverage
- **black**: Code formatting
- **ruff**: Fast Python linter
- **mypy**: Static type checking

## Configuration Management

### User Configuration
Location: `~/.minisky/config.yaml`

```yaml
providers:
  runpod:
    api_key: ${RUNPOD_API_KEY}
  lambda:
    api_key: ${LAMBDA_API_KEY}

defaults:
  provider: mock
  ssh_key_path: ~/.ssh/id_rsa
```

### Environment Variables
- `MINISKY_HOME`: Override default config directory
- `MINISKY_DEBUG`: Enable debug logging
- Provider-specific API keys

## Error Handling Strategy

1. **Validation Errors**: Catch at task parsing stage with clear messages
2. **API Errors**: Retry with exponential backoff, clear error messages
3. **SSH Errors**: Retry connection, provide troubleshooting hints
4. **State Errors**: Graceful degradation, allow manual cleanup

## Testing Strategy

### Unit Tests
- Test each module independently
- Mock external dependencies (SSH, HTTP)
- Test error conditions

### Integration Tests
- Test full workflow with mock provider
- Test CLI commands end-to-end
- Test state persistence

### Manual Testing
- Test with real cloud providers
- Test various task configurations
- Test error recovery

## Security Considerations

1. **API Keys**: Never store in code, use environment variables
2. **SSH Keys**: Use user's existing SSH keys, never generate or store
3. **State Database**: Store in user's home directory with proper permissions
4. **Logs**: Sanitize sensitive information before logging

## Future Enhancements

1. **Multiple Providers**: Add Lambda Cloud, Vast.ai, AWS, GCP
2. **Spot Instances**: Support for cheaper spot/preemptible instances
3. **Auto-scaling**: Launch multiple VMs for parallel tasks
4. **Cost Tracking**: Track and report spending
5. **Job Scheduling**: Queue and schedule tasks
6. **Web Dashboard**: Optional web UI for monitoring
7. **Checkpointing**: Save and resume long-running tasks
8. **Multi-node**: Support for distributed training

## Success Metrics

1. Can launch mock VM in < 1 second
2. Can parse and validate YAML tasks
3. Can track VM state persistently
4. Can execute commands via SSH
5. Clean, typed, tested codebase
6. Clear documentation and examples

## Next Steps

After reviewing this plan:
1. Confirm the architecture meets requirements
2. Switch to Code mode to begin implementation
3. Start with Phase 1 (Foundation with Mock Provider)
4. Follow the development phases sequentially
