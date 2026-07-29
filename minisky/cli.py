"""
Command-line interface for MiniSky.

Provides commands for launching, managing, and terminating cloud VMs.
Includes configuration management, log streaming, and GPU catalog.
"""

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from pathlib import Path
from typing import Optional

from .task import Task
from .state import StateManager
from .providers import get_provider
from .executor import Executor
from .config import MiniSkyConfig
from .logger import LogManager

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

@app.command()
def launch(
    task_file: str = typer.Argument(..., help="Path to task YAML file"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Launch and detach (don't wait for completion)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override provider from task YAML"),
):
    """
    Launch a new task on a cloud VM.

    Example:
        minisky launch task.yaml
        minisky launch task.yaml --detach
        minisky launch task.yaml --provider runpod
    """
    try:
        # Parse task
        console.print(f"[cyan]Loading task from {task_file}...[/cyan]")
        task = Task.from_yaml(task_file)

        # Override provider if specified via CLI
        if provider:
            task.provider = provider

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

        # Launch VM
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(description="Launching VM...", total=None)
            vm_info = cloud.launch(task)

        console.print(f"[green]>[/green] VM launched: {vm_info['vm_id']}")
        console.print(f"  IP: {vm_info['ip_address']}")
        console.print(f"  Provider: {vm_info['provider']}")

        # Save to state
        state.add_vm(vm_info)

        if not detach:
            # Execute task
            console.print("\n[cyan]Executing task...[/cyan]")
            executor = Executor(vm_info)
            executor.execute_task(task)
            console.print("[green]>[/green] Task completed")
        else:
            console.print("\n[yellow]Task launched in detached mode[/yellow]")
            console.print(f"Use 'minisky logs {vm_info['vm_id']}' to view logs")

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
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: int = typer.Option(50, "--tail", "-n", help="Number of lines to show"),
):
    """
    View logs from a running task.

    Example:
        minisky logs mock-abc123
        minisky logs mock-abc123 --follow
        minisky logs mock-abc123 --tail 100
    """
    try:
        vm_info = state.get_vm(vm_id)
        if not vm_info:
            console.print(f"[red]VM not found:[/red] {vm_id}")
            raise typer.Exit(1)

        # Try local logs first
        local_logs = log_manager.read_logs(vm_id, tail=tail)
        if local_logs and not follow:
            console.print(local_logs, end="")
            return

        # Stream from remote
        if vm_info['status'] == 'running':
            console.print(f"[cyan]Streaming logs from {vm_id}...[/cyan]")
            log_manager.stream_logs(vm_info, follow=follow, tail=tail)
        else:
            if local_logs:
                console.print(local_logs, end="")
            else:
                console.print(f"[yellow]No logs available for {vm_id}[/yellow]")

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


if __name__ == "__main__":
    app()

