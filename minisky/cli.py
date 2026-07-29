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
    help="MiniSky - Lightweight cloud orchestration tool",
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
    console.print("This feature will be added in a future release.")


if __name__ == "__main__":
    app()
