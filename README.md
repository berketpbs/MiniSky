# MiniSky

Lightweight cloud orchestration tool inspired by SkyPilot. Run your machine learning and data science workloads easily on multiple cloud providers (RunPod, Lambda Cloud, AWS, GCP) with a single command.

## Features
- **Multi-Cloud Support**: Deploy to RunPod, Lambda Cloud, AWS EC2, GCP Compute Engine, or use the Mock provider for testing.
- **Cost Optimizer**: Automatically selects the cheapest provider for your requirements.
- **File Synchronization**: Automatically sync local directories (`workdir`) to remote VMs.
- **Managed Jobs**: Automatic recovery from spot instance preemptions.
- **CLI & Web Dashboard**: Manage your instances from terminal or a Vue.js web UI.

## Installation

MiniSky isn't published to PyPI yet — install from source with `uv`:

```bash
git clone https://github.com/berketpbs/MiniSky.git
cd MiniSky
uv venv
uv pip sync pyproject.toml
```

## Quick Start

1. Set up your cloud credentials in `~/.minisky/config.yaml`:
```yaml
providers:
  runpod:
    api_key: "YOUR_RUNPOD_KEY"
  lambda:
    api_key: "YOUR_LAMBDA_KEY"
  aws:
    # Optional - if omitted, falls back to the standard AWS credential
    # chain (`aws configure`, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, IAM role)
    access_key_id: "YOUR_AWS_ACCESS_KEY_ID"
    secret_access_key: "YOUR_AWS_SECRET_ACCESS_KEY"
    region: "us-east-1"
    key_name: "your-ec2-keypair-name"       # required to SSH into launched instances
    security_group_id: "sg-xxxxxxxx"        # optional - must allow inbound SSH (22)
  gcp:
    project: "your-gcp-project-id"          # required - GCP has no default project
    # Optional - if omitted, falls back to google-auth's standard chain
    # (GOOGLE_APPLICATION_CREDENTIALS, `gcloud auth application-default login`,
    # or the GCE metadata server)
    credentials_path: "/path/to/service-account.json"
    zone: "us-central1-a"
    ssh_public_key_path: "~/.ssh/id_rsa.pub"  # required to SSH into launched instances
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

> **Note:** the `mock` provider returns `127.0.0.1` as the VM's IP but doesn't run a
> fake SSH server — MiniSky still opens a real SSH connection to execute `setup`/`run`.
> To see a task actually execute end-to-end with `mock`, you need a local SSH server
> listening on port 22 (e.g. Windows OpenSSH Server, or any Linux/macOS machine has one
> by default). Without one, `launch` will time out at the "Waiting for SSH" step —
> state tracking, the CLI, and providers can still be exercised, just not remote execution.

## CLI Reference

### VM lifecycle
```bash
minisky launch task.yaml          # Launch a VM and run the task
minisky status [vm-id]            # Show status of one or all VMs
minisky stop <vm-id>              # Stop a VM, preserving disk
minisky start <vm-id>             # Start a previously stopped VM
minisky terminate <vm-id>         # Terminate a VM and clean up state
```

### Working with a running VM
```bash
minisky exec <vm-id> "nvidia-smi"           # Run a command remotely
minisky ssh <vm-id>                         # Open an interactive SSH session
minisky port-forward <vm-id> jupyter tensorboard   # Forward ports locally
minisky logs <vm-id> -f                     # Stream logs
minisky sync <vm-id> ./local --remote ~/remote     # Sync files to/from a VM (rsync/SFTP)
minisky rsync <vm-id> ./local ~/remote             # Quick rsync shortcut
```

### Fleet-level
```bash
minisky check                     # Verify setup and provider credentials
minisky gpus                      # Browse GPU pricing/availability across providers
minisky cost-report                # Cost report across all tracked VMs
minisky config show|set|get|unset  # Manage ~/.minisky/config.yaml
minisky cluster launch|status|terminate   # Multi-node cluster management
minisky queue list|add|show|cancel|clear  # Job queue management
```

Run `minisky <command> --help` for full options on any command.

## Development

Use `uv` to install dependencies and run tests:
```bash
uv venv
uv pip sync pyproject.toml
uv run pytest tests/
```
