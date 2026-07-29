"""
Command-line interface for MiniSky.

Provides commands for launching, managing, and terminating cloud VMs.
Includes configuration management, log streaming, and GPU catalog.
"""

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from pathlib import Path
from typing import Optional
import time

from .task import Task
from .state import StateManager
from .providers import get_provider
from .executor import Executor
from .config import MiniSkyConfig
from .logger import LogManager
from .provisioner import Provisioner, ProvisionState, ProvisionConfig

app = typer.Typer(
    name="minisky",
    help="MiniSky - Lightweight cloud orchestration tool",
    add_completion=False
)
console = Console()
state = StateManager()
config = MiniSkyConfig()
log_manager = LogManager(config)


# --- Launch ---

def _display_provision_status(state: ProvisionState, elapsed: float) -> Text:
    """Create a rich Text display for provisioning status."""
    state_icons = {
        ProvisionState.PENDING: "⏳",
        ProvisionState.WAITING_SSH: "🔌",
        ProvisionState.INJECTING_KEYS: "🔑",
        ProvisionState.RUNNING_SETUP: "⚙️",
        ProvisionState.RUNNING_TASK: "🚀",
        ProvisionState.COMPLETED: "✅",
        ProvisionState.FAILED: "❌",
    }
    state_colors = {
        ProvisionState.PENDING: "yellow",
        ProvisionState.WAITING_SSH: "cyan",
        ProvisionState.INJECTING_KEYS: "blue",
        ProvisionState.RUNNING_SETUP: "magenta",
        ProvisionState.RUNNING_TASK: "green",
        ProvisionState.COMPLETED: "green",
        ProvisionState.FAILED: "red",
    }
    state_labels = {
        ProvisionState.PENDING: "Pending",
        ProvisionState.WAITING_SSH: "Waiting for SSH",
        ProvisionState.INJECTING_KEYS: "Injecting SSH keys",
        ProvisionState.RUNNING_SETUP: "Running setup",
        ProvisionState.RUNNING_TASK: "Running task",
        ProvisionState.COMPLETED: "Completed",
        ProvisionState.FAILED: "Failed",
    }
    
    icon = state_icons.get(state, "❓")
    color = state_colors.get(state, "white")
    label = state_labels.get(state, str(state))
    
    text = Text()
    text.append(f"{icon} ", style="bold")
    text.append(f"{label}", style=f"bold {color}")
    text.append(f" ({elapsed:.1f}s)", style="dim")
    return text


@app.command()
def launch(
    task_file: str = typer.Argument(..., help="Path to task YAML file"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Launch and detach (don't wait for completion)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override provider from task YAML"),
    optimize: bool = typer.Option(False, "--optimize", "-o", help="Auto-select cheapest provider"),
    spot: bool = typer.Option(False, "--spot", "-s", help="Request spot/preemptible instance"),
    skip_setup: bool = typer.Option(False, "--skip-setup", help="Skip setup commands"),
):
    """
    Launch a new task on a cloud VM.

    Example:
        minisky launch task.yaml
        minisky launch task.yaml --optimize
        minisky launch task.yaml --provider runpod --spot
    """
    try:
        # Parse task
        console.print(f"[cyan]Loading task from {task_file}...[/cyan]")
        task = Task.from_yaml(task_file)

        # Override spot if specified via CLI
        if spot:
            task.resources.use_spot = True

        # Override provider if specified via CLI
        if provider:
            task.provider = provider

        # Cost optimizer: find cheapest provider
        if optimize and not provider:
            from .optimizer import CostOptimizer
            optimizer = CostOptimizer(config)
            console.print("[cyan]Searching for cheapest option...[/cyan]")
            best = optimizer.find_best(task, prefer_spot=task.resources.use_spot)
            if best and best.available and best.provider != 'mock':
                task.provider = best.provider
                console.print(
                    f"[green]>[/green] Optimizer selected: [bold]{best.provider}[/bold] "
                    f"({best.gpu_name}) at ${best.effective_price:.2f}/hr"
                )
            else:
                console.print("[yellow]No cheaper real provider found, using task default[/yellow]")

        console.print(f"[green]>[/green] Task '{task.name}' loaded")
        console.print(f"  Provider: {task.provider}")
        if task.resources.gpu:
            console.print(f"  GPU: {task.resources.gpu} x{task.resources.gpu_count}")
        if task.resources.use_spot:
            console.print(f"  Spot: [yellow]enabled[/yellow]")
        if task.num_nodes > 1:
            console.print(f"  Nodes: {task.num_nodes}")

        # Get provider
        cloud = get_provider(task.provider)

        # Phase 1: Launch VM
        console.print("\n[bold]Phase 1/4: Launching VM[/bold]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Provisioning VM...", total=None)
            vm_info = cloud.launch(task)

        console.print(f"[green]✓[/green] VM launched: {vm_info['vm_id']}")
        console.print(f"  IP: {vm_info['ip_address']}")
        console.print(f"  Provider: {vm_info['provider']}")

        # Save to state
        state.add_vm(vm_info)

        # Register autostop if configured
        autostop_minutes = task.autostop_minutes or config.get('autostop_minutes')
        if autostop_minutes and detach:
            from .autostop import AutostopManager
            autostop = AutostopManager(config, state)
            autostop.register(vm_info['vm_id'], autostop_minutes)
            autostop.start_daemon()

        if detach:
            console.print("\n[yellow]Task launched in detached mode[/yellow]")
            console.print(f"Use 'minisky logs {vm_info['vm_id']}' to view logs")
            return

        # Phase 2: Wait for SSH and provision
        console.print("\n[bold]Phase 2/4: Establishing SSH connection[/bold]")
        
        # Create provisioner for SSH lifecycle management with custom config
        provision_config = ProvisionConfig(
            ssh_timeout=120,
            ssh_retry_interval=5,
            stream_logs=True
        )
        provisioner = Provisioner(vm_info, config=provision_config)
        start_time = time.time()
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            ssh_task = progress.add_task(description="Waiting for SSH to be ready...", total=None)
            
            if not provisioner.wait_for_ssh():
                console.print("[red]✗[/red] Failed to establish SSH connection")
                console.print("  The VM may still be booting. Try again with:")
                console.print(f"  minisky exec {vm_info['vm_id']} -- <command>")
                raise typer.Exit(1)
        
        ssh_elapsed = time.time() - start_time
        console.print(f"[green]✓[/green] SSH connection established ({ssh_elapsed:.1f}s)")

        # Phase 3: Run setup commands
        setup_commands = task.setup if not skip_setup else None
        if setup_commands:
            console.print(f"\n[bold]Phase 3/4: Running setup ({len(setup_commands)} commands)[/bold]")
            
            for i, cmd in enumerate(setup_commands, 1):
                console.print(f"\n[cyan]Setup {i}/{len(setup_commands)}:[/cyan] {cmd}")
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console
                ) as progress:
                    progress.add_task(description="Executing...", total=None)
                    success, output = provisioner.run_setup([cmd])
                
                if success:
                    console.print(f"[green]✓[/green] Setup {i} completed")
                else:
                    console.print(f"[red]✗[/red] Setup {i} failed")
                    if output:
                        console.print(f"  Output: {output}")
                    raise typer.Exit(1)
        else:
            console.print("\n[bold]Phase 3/4: Setup[/bold] [dim](skipped)[/dim]")

        # Phase 4: Run main task
        console.print(f"\n[bold]Phase 4/4: Running task ({len(task.run)} commands)[/bold]")
        
        # Sync workdir if specified
        if task.workdir:
            console.print(f"\n[cyan]Syncing workdir:[/cyan] {task.workdir}")
            executor = Executor(vm_info)
            executor.connect()
            try:
                executor.sync_files(task.workdir, remote_path="~/workdir")
            finally:
                executor.disconnect()
        
        # Run task commands
        for i, cmd in enumerate(task.run, 1):
            console.print(f"\n[green]Run {i}/{len(task.run)}:[/green] {cmd}")
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                progress.add_task(description="Executing...", total=None)
                exit_code, output = provisioner.run_task(cmd, env=task.env)
            
            if exit_code == 0:
                console.print(f"[green]✓[/green] Command {i} completed")
            else:
                console.print(f"[red]✗[/red] Command {i} failed with exit code {exit_code}")
                if output:
                    console.print(f"  Output: {output}")
                raise typer.Exit(1)

        # Summary
        total_elapsed = time.time() - start_time
        console.print(f"\n[bold green]✓ Task completed successfully[/bold green]")
        console.print(f"  Total time: {total_elapsed:.1f}s")
        console.print(f"  VM ID: {vm_info['vm_id']}")
        console.print(f"\nTo terminate: minisky terminate {vm_info['vm_id']}")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Status ---

@app.command()
def status(
    vm_id: Optional[str] = typer.Argument(None, help="Specific VM ID to check")
):
    """
    Show status of VMs.

    Example:
        minisky status
        minisky status mock-abc123
    """
    try:
        if vm_id:
            # Show specific VM
            vm_info = state.get_vm(vm_id)
            if not vm_info:
                console.print(f"[red]VM not found:[/red] {vm_id}")
                raise typer.Exit(1)

            panel_content = (
                f"[bold]VM ID:[/bold]    {vm_id}\n"
                f"[bold]Status:[/bold]   {vm_info['status']}\n"
                f"[bold]IP:[/bold]       {vm_info['ip_address']}\n"
                f"[bold]Provider:[/bold] {vm_info['provider']}\n"
                f"[bold]Task:[/bold]     {vm_info['task_name']}\n"
                f"[bold]Created:[/bold]  {vm_info['created_at']}"
            )
            console.print(Panel(panel_content, title=f"VM {vm_id}", border_style="cyan"))
        else:
            # Show all VMs
            vms = state.list_vms()

            if not vms:
                console.print("[yellow]No VMs found[/yellow]")
                return

            table = Table(title="MiniSky Instances")
            table.add_column("VM ID", style="cyan", no_wrap=True)
            table.add_column("Task", style="magenta")
            table.add_column("Provider", style="blue")
            table.add_column("IP Address", style="green")
            table.add_column("Status", style="yellow")

            for vm in vms:
                status_style = {
                    'running': '[green]running[/green]',
                    'stopped': '[yellow]stopped[/yellow]',
                    'terminated': '[red]terminated[/red]',
                }.get(vm['status'], vm['status'])

                table.add_row(
                    vm['vm_id'],
                    vm['task_name'],
                    vm['provider'],
                    vm['ip_address'],
                    status_style
                )

            console.print(table)

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Terminate ---

@app.command()
def terminate(
    vm_id: str = typer.Argument(..., help="VM ID to terminate"),
    force: bool = typer.Option(False, "--force", "-f", help="Force termination without confirmation")
):
    """
    Terminate a VM instance and remove all associated resources.

    Example:
        minisky terminate mock-abc123
        minisky terminate mock-abc123 --force
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
        cloud = get_provider(vm_info['provider'])

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Terminating VM...", total=None)
            cloud.terminate(vm_id)

        # Update state
        state.remove_vm(vm_id)

        console.print(f"[green]>[/green] VM terminated: {vm_id}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Stop ---

@app.command()
def stop(
    vm_id: str = typer.Argument(..., help="VM ID to stop")
):
    """
    Stop a running VM (preserves disk, saves cost).

    Example:
        minisky stop mock-abc123
    """
    try:
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)

        if vm_info['status'] != 'running':
            console.print(f"[yellow]VM is not running (current: {vm_info['status']})[/yellow]")
            return

        cloud = get_provider(vm_info['provider'])

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Stopping VM...", total=None)
            if hasattr(cloud, 'stop'):
                cloud.stop(vm_id)
            else:
                console.print(f"[yellow]Provider '{vm_info['provider']}' does not support stop. Use terminate instead.[/yellow]")
                return

        state.update_status(vm_id, 'stopped')
        console.print(f"[green]>[/green] VM stopped: {vm_id}")
        console.print("  Disk preserved. Use 'minisky start' to resume.")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Start ---

@app.command()
def start(
    vm_id: str = typer.Argument(..., help="VM ID to start")
):
    """
    Start a previously stopped VM.

    Example:
        minisky start mock-abc123
    """
    try:
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)

        if vm_info['status'] != 'stopped':
            console.print(f"[yellow]VM is not stopped (current: {vm_info['status']})[/yellow]")
            return

        cloud = get_provider(vm_info['provider'])

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Starting VM...", total=None)
            if hasattr(cloud, 'start'):
                cloud.start(vm_id)
            else:
                console.print(f"[yellow]Provider '{vm_info['provider']}' does not support start.[/yellow]")
                return

        state.update_status(vm_id, 'running')
        console.print(f"[green]>[/green] VM started: {vm_id}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Exec ---

@app.command(name="exec")
def exec_cmd(
    vm_id: str = typer.Argument(..., help="VM ID to execute on"),
    command: str = typer.Argument(..., help="Command to execute"),
):
    """
    Execute a command on a running VM.

    Example:
        minisky exec mock-abc123 "python train.py --epochs 50"
        minisky exec mock-abc123 "nvidia-smi"
    """
    try:
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)

        if vm_info['status'] != 'running':
            console.print(f"[yellow]VM is not running (current: {vm_info['status']})[/yellow]")
            raise typer.Exit(1)

        console.print(f"[cyan]Executing on {vm_id}:[/cyan] {command}")

        executor = Executor(vm_info)
        executor.connect()
        try:
            exit_code = executor.execute_command(command)
            if exit_code == 0:
                console.print(f"[green]>[/green] Command completed (exit code: 0)")
            else:
                console.print(f"[red]Command failed (exit code: {exit_code})[/red]")
                raise typer.Exit(exit_code)
        finally:
            executor.disconnect()

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Logs ---

@app.command()
def logs(
    vm_id: str = typer.Argument(..., help="VM ID to view logs"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output in real-time"),
    tail: int = typer.Option(50, "--tail", "-n", help="Number of lines to show"),
    log_file: str = typer.Option("/tmp/minisky_task.log", "--file", help="Remote log file path"),
    timestamps: bool = typer.Option(True, "--timestamps/--no-timestamps", help="Show timestamps"),
):
    """
    View logs from a running task.

    Supports real-time streaming with --follow flag.

    Example:
        minisky logs mock-abc123
        minisky logs mock-abc123 --follow
        minisky logs mock-abc123 --tail 100
        minisky logs mock-abc123 --file /var/log/app.log
    """
    try:
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)

        # Try local logs first (for non-follow mode)
        local_logs = log_manager.read_logs(vm_id, tail=tail)
        if local_logs and not follow:
            console.print(local_logs, end="")
            return

        # Stream from remote
        if vm_info['status'] == 'running':
            from .log_streamer import SSHLogStreamer
            
            streamer = SSHLogStreamer(vm_info, [log_file])
            streamer.stream_to_console(
                follow=follow,
                tail=tail,
                show_timestamps=timestamps
            )
            
            # Also save to local logs
            if follow:
                log_manager.write_log(vm_id, f"[Log streaming session ended]")
        else:
            if local_logs:
                console.print(local_logs, end="")
            else:
                console.print(f"[yellow]No logs available for {vm_id}[/yellow]")
                console.print(f"[dim]VM status: {vm_info['status']}[/dim]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Log streaming stopped[/yellow]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        raise typer.Exit(1)


# --- Config ---

config_app = typer.Typer(help="Manage MiniSky configuration")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show():
    """
    Display current configuration.

    Example:
        minisky config show
    """
    import yaml as _yaml
    data = config.show()
    console.print(Panel(
        _yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip(),
        title="MiniSky Configuration",
        border_style="cyan"
    ))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dot notation, e.g. providers.runpod.api_key)"),
    value: str = typer.Argument(..., help="Value to set"),
):
    """
    Set a configuration value.

    Example:
        minisky config set default_provider runpod
        minisky config set providers.runpod.api_key rp_xxxxx
    """
    config.set(key, value)
    console.print(f"[green]>[/green] Set {key} = {value}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key (dot notation)"),
):
    """
    Get a configuration value.

    Example:
        minisky config get default_provider
    """
    value = config.get(key)
    if value is None:
        console.print(f"[yellow]Key not found:[/yellow] {key}")
    else:
        console.print(f"{key} = {value}")


@config_app.command("unset")
def config_unset(
    key: str = typer.Argument(..., help="Config key to remove"),
):
    """
    Remove a configuration value.

    Example:
        minisky config unset providers.runpod.api_key
    """
    if config.unset(key):
        console.print(f"[green]>[/green] Removed {key}")
    else:
        console.print(f"[yellow]Key not found:[/yellow] {key}")


# --- Check ---

@app.command()
def check():
    """
    Verify MiniSky setup and provider credentials.

    Example:
        minisky check
    """
    console.print(Panel("[bold]MiniSky Setup Check[/bold]", border_style="cyan"))

    # Config file
    config_path = config._config_path
    if config_path.exists():
        console.print(f"[green]>[/green] Config file: {config_path}")
    else:
        console.print(f"[yellow]![/yellow] Config file not found (using defaults)")

    # State database
    state_path = Path(state.db_path)
    if state_path.exists():
        console.print(f"[green]>[/green] State DB: {state_path}")
    else:
        console.print(f"[yellow]![/yellow] State DB not found (will be created)")

    # Providers
    console.print("\n[bold]Providers:[/bold]")

    # Mock
    console.print(f"  [green]>[/green] mock: Ready (no credentials required)")

    # RunPod
    runpod_key = config.get('providers.runpod.api_key')
    if runpod_key:
        console.print(f"  [green]>[/green] runpod: Configured (key: {runpod_key[:8]}...)")
    else:
        console.print(f"  [red]x[/red] runpod: Not configured (set providers.runpod.api_key)")

    # Lambda
    lambda_key = config.get('providers.lambda.api_key')
    if lambda_key:
        console.print(f"  [green]>[/green] lambda: Configured (key: {lambda_key[:8]}...)")
    else:
        console.print(f"  [red]x[/red] lambda: Not configured (set providers.lambda.api_key)")

    # SSH
    console.print("\n[bold]SSH:[/bold]")
    ssh_key = config.get('ssh.default_key_path')
    if ssh_key and Path(ssh_key).expanduser().exists():
        console.print(f"  [green]>[/green] Default key: {ssh_key}")
    elif ssh_key:
        console.print(f"  [red]x[/red] Default key not found: {ssh_key}")
    else:
        default_key = Path.home() / '.ssh' / 'id_rsa'
        if default_key.exists():
            console.print(f"  [green]>[/green] Default key: {default_key}")
        else:
            console.print(f"  [yellow]![/yellow] No SSH key configured (will use ssh-agent)")


# --- GPUs ---

@app.command()
def gpus(
    gpu_filter: Optional[str] = typer.Argument(None, help="Filter by GPU name (e.g. A100, H100)"),
    available_only: bool = typer.Option(False, "--available", "-a", help="Only show available GPUs"),
):
    """
    Browse GPU pricing and availability across all providers.

    Example:
        minisky gpus
        minisky gpus A100
        minisky gpus --available
    """
    from .catalog import GPUCatalog
    catalog = GPUCatalog(config)
    catalog.display(gpu_filter=gpu_filter, available_only=available_only)

# --- Queue ---

queue_app = typer.Typer(help="Job queue management")
app.add_typer(queue_app, name="queue")


@queue_app.command("list")
def queue_list(
    vm_id: Optional[str] = typer.Argument(None, help="Filter by VM ID"),
    status_filter: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status (pending, running, completed, failed)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of jobs to show"),
):
    """
    List jobs in the queue.

    Example:
        minisky queue list
        minisky queue list mock-abc123
        minisky queue list --status running
    """
    from .queue import JobQueue, JobStatus
    
    job_queue = JobQueue()
    
    # Parse status filter
    status = None
    if status_filter:
        try:
            status = JobStatus(status_filter.lower())
        except ValueError:
            console.print(f"[red]Invalid status:[/red] {status_filter}")
            console.print(f"Valid options: {', '.join(s.value for s in JobStatus)}")
            raise typer.Exit(1)
    
    jobs = job_queue.list_jobs(vm_id=vm_id, status=status, limit=limit)
    
    if not jobs:
        console.print("[yellow]No jobs found[/yellow]")
        return
    
    table = Table(title="Job Queue")
    table.add_column("Job ID", style="cyan")
    table.add_column("VM ID", style="blue")
    table.add_column("Command", style="white", max_width=40)
    table.add_column("Status", style="yellow")
    table.add_column("Created", style="dim")
    table.add_column("Duration", style="green")
    
    for job in jobs:
        # Format status with color
        status_str = job.status.value
        if job.status == JobStatus.RUNNING:
            status_str = f"[green]{status_str}[/green]"
        elif job.status == JobStatus.FAILED:
            status_str = f"[red]{status_str}[/red]"
        elif job.status == JobStatus.COMPLETED:
            status_str = f"[blue]{status_str}[/blue]"
        
        # Format duration
        duration_str = ""
        if job.duration:
            duration_str = f"{job.duration:.1f}s"
        
        table.add_row(
            job.job_id,
            job.vm_id[:12],
            job.command[:40] + ("..." if len(job.command) > 40 else ""),
            status_str,
            job.created_at_str,
            duration_str
        )
    
    console.print(table)
    
    # Show stats
    stats = job_queue.get_stats(vm_id)
    console.print(f"\n[dim]Total: {stats['total']} | Pending: {stats['pending']} | Running: {stats['running']} | Completed: {stats['completed']} | Failed: {stats['failed']}[/dim]")


@queue_app.command("add")
def queue_add(
    vm_id: str = typer.Argument(..., help="VM ID to run job on"),
    command: str = typer.Argument(..., help="Command to execute"),
    run_now: bool = typer.Option(False, "--run", "-r", help="Execute immediately"),
):
    """
    Add a job to the queue.

    Example:
        minisky queue add mock-abc123 "python train.py"
        minisky queue add mock-abc123 "nvidia-smi" --run
    """
    from .queue import JobQueue
    
    # Verify VM exists
    vm_info = state.get_vm(vm_id)
    if not vm_info:
        console.print(f"[red]VM not found:[/red] {vm_id}")
        raise typer.Exit(1)
    
    job_queue = JobQueue()
    job = job_queue.add_job(vm_id, command)
    
    console.print(f"[green]>[/green] Job added: {job.job_id}")
    console.print(f"  Command: {command}")
    console.print(f"  Status: {job.status.value}")
    
    if run_now:
        if vm_info['status'] != 'running':
            console.print(f"[yellow]VM is not running (status: {vm_info['status']})[/yellow]")
            return
        
        console.print("\n[cyan]Executing job...[/cyan]")
        job_queue.mark_running(job.job_id)
        
        try:
            executor = Executor(vm_info)
            executor.connect()
            exit_code = executor.execute_command(command)
            executor.disconnect()
            
            if exit_code == 0:
                job_queue.mark_completed(job.job_id, exit_code=exit_code)
                console.print(f"[green]>[/green] Job completed successfully")
            else:
                job_queue.mark_failed(job.job_id, exit_code=exit_code)
                console.print(f"[red]>[/red] Job failed with exit code {exit_code}")
        except Exception as e:
            job_queue.mark_failed(job.job_id, error=str(e))
            console.print(f"[red]Error:[/red] {str(e)}")


@queue_app.command("show")
def queue_show(
    job_id: str = typer.Argument(..., help="Job ID to show"),
):
    """
    Show details of a specific job.

    Example:
        minisky queue show job-mock-abc12345
    """
    from .queue import JobQueue
    
    job_queue = JobQueue()
    job = job_queue.get_job(job_id)
    
    if not job:
        console.print(f"[red]Job not found:[/red] {job_id}")
        raise typer.Exit(1)
    
    console.print(Panel(f"[bold]Job: {job.job_id}[/bold]", border_style="cyan"))
    console.print(f"  VM ID: {job.vm_id}")
    console.print(f"  Command: {job.command}")
    console.print(f"  Status: {job.status.value}")
    console.print(f"  Created: {job.created_at_str}")
    
    if job.started_at:
        from datetime import datetime
        console.print(f"  Started: {datetime.fromtimestamp(job.started_at).strftime('%Y-%m-%d %H:%M:%S')}")
    
    if job.completed_at:
        from datetime import datetime
        console.print(f"  Completed: {datetime.fromtimestamp(job.completed_at).strftime('%Y-%m-%d %H:%M:%S')}")
    
    if job.duration:
        console.print(f"  Duration: {job.duration:.2f}s")
    
    if job.exit_code is not None:
        console.print(f"  Exit Code: {job.exit_code}")
    
    if job.output:
        console.print(f"\n[bold]Output:[/bold]")
        console.print(job.output)
    
    if job.error:
        console.print(f"\n[bold red]Error:[/bold red]")
        console.print(job.error)


@queue_app.command("cancel")
def queue_cancel(
    job_id: str = typer.Argument(..., help="Job ID to cancel"),
):
    """
    Cancel a pending job.

    Example:
        minisky queue cancel job-mock-abc12345
    """
    from .queue import JobQueue
    
    job_queue = JobQueue()
    
    if job_queue.cancel_job(job_id):
        console.print(f"[green]>[/green] Job cancelled: {job_id}")
    else:
        console.print(f"[yellow]Cannot cancel job (not pending or not found):[/yellow] {job_id}")


@queue_app.command("clear")
def queue_clear(
    vm_id: str = typer.Argument(..., help="VM ID to clear jobs for"),
    status_filter: Optional[str] = typer.Option(None, "--status", "-s", help="Only clear jobs with this status"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """
    Clear jobs for a VM.

    Example:
        minisky queue clear mock-abc123
        minisky queue clear mock-abc123 --status completed
    """
    from .queue import JobQueue, JobStatus
    
    job_queue = JobQueue()
    
    status = None
    if status_filter:
        try:
            status = JobStatus(status_filter.lower())
        except ValueError:
            console.print(f"[red]Invalid status:[/red] {status_filter}")
            raise typer.Exit(1)
    
    if not force:
        msg = f"Clear all jobs for VM {vm_id}"
        if status:
            msg += f" with status '{status.value}'"
        msg += "?"
        
        if not typer.confirm(msg):
            console.print("[yellow]Cancelled[/yellow]")
            return
    
    count = job_queue.clear_vm_jobs(vm_id, status)
    console.print(f"[green]>[/green] Cleared {count} jobs")


if __name__ == "__main__":
    app()


