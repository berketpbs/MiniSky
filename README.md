# MiniSky

Lightweight cloud orchestration tool inspired by SkyPilot. Run your machine learning and data science workloads easily on multiple cloud providers (RunPod, Lambda Cloud) with a single command.

## Features
- **Multi-Cloud Support**: Deploy to RunPod, Lambda Cloud, or use the Mock provider for testing.
- **Cost Optimizer**: Automatically selects the cheapest provider for your requirements.
- **File Synchronization**: Automatically sync local directories (`workdir`) to remote VMs.
- **Managed Jobs**: Automatic recovery from spot instance preemptions.
- **CLI & Web Dashboard**: Manage your instances from terminal or a Vue.js web UI.

## Installation

```bash
pip install minisky
```

## Quick Start

1. Set up your cloud credentials in `~/.minisky/config.yaml`:
```yaml
providers:
  runpod:
    api_key: "YOUR_RUNPOD_KEY"
  lambda:
    api_key: "YOUR_LAMBDA_KEY"
```

2. Create a task YAML file (`task.yaml`):
```yaml
name: my-training-job
resources:
  gpu: "A100"
  gpu_count: 1
  disk_gb: 100
workdir: ./src
run:
  - python train.py
```

3. Launch your task:
```bash
minisky launch task.yaml
```

## Advanced CLI Commands

```bash
# Check status of running VMs
minisky status

# Forward ports (e.g. Jupyter or Tensorboard)
minisky port-forward <vm-id> jupyter tensorboard

# Open interactive SSH session
minisky ssh <vm-id>

# Run command remotely
minisky exec <vm-id> "nvidia-smi"

# View logs
minisky logs <vm-id> -f

# Add job to queue
minisky queue add <vm-id> "python evaluate.py"

# Terminate VM
minisky terminate <vm-id>
```

## Development

Use `uv` to install dependencies and run tests:
```bash
uv venv
uv pip sync pyproject.toml
uv run pytest tests/
```
