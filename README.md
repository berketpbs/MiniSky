# MiniSky

A lightweight, open-source cloud orchestration tool inspired by SkyPilot. Launch and manage GPU instances across various cloud providers with a simple YAML interface.

## Features

- **YAML Interface**: Define tasks, resources, and environment variables in a clean, readable declarative format.
- **Provider Agnostic**: Native support for multiple cloud environments including local mock testing, with architecture designed for RunPod and Lambda Cloud integration.
- **State Management**: Persistent local VM tracking and state management using SQLite.
- **SSH Execution**: Seamless remote command execution, automated environment setup, and SFTP file synchronization via Paramiko.
- **CLI Dashboard**: Rich, interactive terminal output with progress indicators, built on Typer and Rich.
- **Local Simulation**: Built-in mock provider for end-to-end testing without incurring cloud infrastructure costs.

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/minisky.git
cd minisky

# Install using uv (Recommended)
uv pip install -e .
```

### 2. Define a Task

Create a file named `task.yaml`:

```yaml
name: model-training
provider: mock
resources:
  gpu: A100
  gpu_count: 1
  memory_gb: 32
  disk_gb: 50
workdir: ./my-project
setup:
  - pip install -r requirements.txt
run:
  - python train.py --epochs 100
env:
  WANDB_API_KEY: ${WANDB_API_KEY}
```

### 3. Launch and Manage

```bash
# Launch the instance and execute the task
minisky launch task.yaml

# Check the status of all active instances
minisky status

# Terminate an instance when finished
minisky terminate mock-abc123
```

## CLI Reference

- `minisky launch <file> [--detach]`: Launch a task on a remote VM. Use `--detach` to run without waiting for completion.
- `minisky status [id]`: View the status of a specific VM or list all active VMs.
- `minisky terminate <id> [--force]`: Terminate a running VM instance and clean up state.
- `minisky logs <id> [--follow]`: Stream logs from a running task (Planned).

## Architecture

- **CLI Layer**: Built with Typer for command parsing and Rich for terminal rendering.
- **Task Parser**: YAML configuration parsing and strict validation using Pydantic models.
- **State Manager**: Local SQLite database (`~/.minisky/state.db`) for tracking VM metadata and lifecycle states.
- **Provider Layer**: Hexagonal abstract provider interface. Currently implements `mock.py` for local simulation.
- **Executor**: SSH and SFTP orchestration using Paramiko for reliable remote command execution and workspace synchronization.

## Roadmap

- Core Architecture & State Management (Completed)
- YAML Parser & Basic CLI (Completed)
- Mock Provider (Completed)
- SSH Executor (In Progress)
- RunPod Provider Integration (Planned)
- Lambda Cloud Provider Integration (Planned)
- Log Streaming & Cost Tracking (Planned)
- Multi-node Task Support (Planned)

## Development & Testing

Setup the development environment and run the test suite:

```bash
# Install development dependencies
uv pip install -e ".[dev]"

# Run unit and integration tests
pytest

# Run with coverage report
pytest --cov=minisky --cov-report=html

# Code formatting and linting
black minisky/ tests/
ruff check minisky/ tests/
mypy minisky/
```

## License

This project is licensed under the MIT License. See the LICENSE file for details.
