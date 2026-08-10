"""
SSH and Port Forwarding Module for MiniSky.

Provides direct SSH access and port forwarding for services
like Jupyter, TensorBoard, etc.
"""

import subprocess
import sys
import os
import signal
import threading
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from rich.console import Console

console = Console()


@dataclass
class PortForward:
    """Represents a port forwarding configuration."""
    local_port: int
    remote_host: str = "localhost"
    remote_port: Optional[int] = None
    
    def __post_init__(self):
        if self.remote_port is None:
            self.remote_port = self.local_port
    
    def to_ssh_arg(self) -> str:
        """Convert to SSH -L argument format."""
        return f"{self.local_port}:{self.remote_host}:{self.remote_port}"
    
    @classmethod
    def parse(cls, spec: str) -> "PortForward":
        """
        Parse port forward specification.
        
        Formats:
            8080              -> 8080:localhost:8080
            8080:80           -> 8080:localhost:80
            8080:host:80      -> 8080:host:80
        """
        parts = spec.split(":")
        
        if len(parts) == 1:
            port = int(parts[0])
            return cls(local_port=port, remote_port=port)
        elif len(parts) == 2:
            return cls(local_port=int(parts[0]), remote_port=int(parts[1]))
        elif len(parts) == 3:
            return cls(
                local_port=int(parts[0]),
                remote_host=parts[1],
                remote_port=int(parts[2])
            )
        else:
            raise ValueError(f"Invalid port forward spec: {spec}")


@dataclass
class SSHConfig:
    """SSH connection configuration."""
    host: str
    port: int = 22
    user: str = "root"
    key_path: Optional[str] = None
    connect_timeout: int = 30
    server_alive_interval: int = 60
    strict_host_key_checking: bool = False


class SSHManager:
    """
    Manages SSH connections and port forwarding.
    
    Usage:
        ssh = SSHManager(vm_info)
        ssh.connect()  # Interactive SSH session
        ssh.connect(command="nvidia-smi")  # Run command
        ssh.forward_ports([8888, 6006])  # Forward Jupyter + TensorBoard
    """
    
    def __init__(self, vm_info: Dict[str, Any], config_manager: Optional[Any] = None):
        """
        Initialize SSH manager.
        
        Args:
            vm_info: VM information dictionary
            config_manager: MiniSky config manager for SSH settings
        """
        self.vm_info = vm_info
        self.config_manager = config_manager
        
        # Build SSH config
        self.ssh_config = SSHConfig(
            host=vm_info['ip_address'],
            port=vm_info.get('ssh_port', 22),
            user=vm_info.get('ssh_user', 'root'),
            key_path=vm_info.get('ssh_key_path')
        )
        
        # Try to get default key from config
        if not self.ssh_config.key_path and config_manager:
            default_key = config_manager.get('ssh.default_key_path')
            if default_key:
                self.ssh_config.key_path = default_key
        
        # Active port forward processes
        self._forward_processes: Dict[int, subprocess.Popen] = {}
    
    def _build_ssh_command(
        self,
        command: Optional[str] = None,
        port_forwards: Optional[List[PortForward]] = None,
        extra_args: Optional[List[str]] = None
    ) -> List[str]:
        """
        Build SSH command with all options.
        
        Args:
            command: Optional command to run
            port_forwards: List of port forwards
            extra_args: Additional SSH arguments
            
        Returns:
            List of command arguments
        """
        cmd = ["ssh"]
        
        # Connection options
        cmd.extend(["-p", str(self.ssh_config.port)])
        cmd.extend(["-o", f"ConnectTimeout={self.ssh_config.connect_timeout}"])
        cmd.extend(["-o", f"ServerAliveInterval={self.ssh_config.server_alive_interval}"])
        
        if not self.ssh_config.strict_host_key_checking:
            cmd.extend(["-o", "StrictHostKeyChecking=no"])
            cmd.extend(["-o", "UserKnownHostsFile=/dev/null"])
        
        # SSH key
        if self.ssh_config.key_path:
            key_path = Path(self.ssh_config.key_path).expanduser()
            if key_path.exists():
                cmd.extend(["-i", str(key_path)])
        
        # Port forwards
        if port_forwards:
            for pf in port_forwards:
                cmd.extend(["-L", pf.to_ssh_arg()])
        
        # Extra arguments
        if extra_args:
            cmd.extend(extra_args)
        
        # User@host
        cmd.append(f"{self.ssh_config.user}@{self.ssh_config.host}")
        
        # Command to run
        if command:
            cmd.append(command)
        
        return cmd
    
    def connect(
        self,
        command: Optional[str] = None,
        port_forwards: Optional[List[PortForward]] = None,
        extra_args: Optional[List[str]] = None,
        auto_reconnect: bool = False,
        max_retries: int = 3
    ) -> int:
        """
        Open SSH connection (interactive or run command).
        
        Args:
            command: Optional command to run (interactive if None)
            port_forwards: List of port forwards
            extra_args: Additional SSH arguments
            auto_reconnect: Whether to automatically reconnect on connection drop
            max_retries: Maximum number of reconnection attempts
            
        Returns:
            Exit code
        """
        cmd = self._build_ssh_command(command, port_forwards, extra_args)
        
        if command:
            console.print(f"[dim]Running on {self.ssh_config.host}: {command}[/dim]")
        else:
            console.print(f"[cyan]Connecting to {self.ssh_config.host}...[/cyan]")
            console.print("[dim]Starting interactive session[/dim]")
        
        if port_forwards:
            for pf in port_forwards:
                console.print(f"[green]✓[/green] Port forward: localhost:{pf.local_port} -> {pf.remote_host}:{pf.remote_port}")
        
        # Run SSH with retry loop
        retries = 0
        while True:
            try:
                result = subprocess.run(cmd)
                
                # Exit code 255 usually means SSH connection error/dropped
                if result.returncode == 255 and auto_reconnect and retries < max_retries:
                    retries += 1
                    console.print(f"\n[yellow]Connection dropped (Code 255). Reconnecting (Attempt {retries}/{max_retries})...[/yellow]")
                    import time
                    time.sleep(2)
                    continue
                    
                return result.returncode
                
            except KeyboardInterrupt:
                console.print("\n[yellow]Connection closed[/yellow]")
                return 0
    
    def forward_ports(
        self,
        ports: List[int],
        background: bool = True
    ) -> Dict[int, bool]:
        """
        Setup port forwarding for multiple ports.
        
        Args:
            ports: List of ports to forward
            background: Run in background
            
        Returns:
            Dictionary mapping port to success status
        """
        results = {}
        port_forwards = [PortForward(local_port=p) for p in ports]
        
        if not background:
            # Foreground mode - blocks until Ctrl+C
            self.connect(port_forwards=port_forwards, extra_args=["-N"])
            return {p: True for p in ports}
        
        # Background mode - spawn process
        cmd = self._build_ssh_command(port_forwards=port_forwards, extra_args=["-N", "-f"])
        
        console.print("[cyan]Setting up port forwards...[/cyan]")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait a bit to check if it started successfully
            import time
            time.sleep(1)
            
            if process.poll() is None:
                # Process is running
                for pf in port_forwards:
                    self._forward_processes[pf.local_port] = process
                    results[pf.local_port] = True
                    console.print(f"[green]✓[/green] Forwarding port {pf.local_port}")
            else:
                # Process exited
                stderr = process.stderr.read().decode() if process.stderr else ""
                console.print(f"[red]Port forwarding failed:[/red] {stderr}")
                for p in ports:
                    results[p] = False
                    
        except Exception as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            for p in ports:
                results[p] = False
        
        return results
    
    def stop_forwards(self, ports: Optional[List[int]] = None):
        """
        Stop port forwarding.
        
        Args:
            ports: Specific ports to stop (all if None)
        """
        if ports is None:
            ports = list(self._forward_processes.keys())
        
        for port in ports:
            if port in self._forward_processes:
                process = self._forward_processes[port]
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                
                del self._forward_processes[port]
                console.print(f"[yellow]Stopped forwarding port {port}[/yellow]")
    
    def copy_file(
        self,
        local_path: str,
        remote_path: str,
        to_remote: bool = True
    ) -> bool:
        """
        Copy file using SCP.
        
        Args:
            local_path: Local file path
            remote_path: Remote file path
            to_remote: True to upload, False to download
            
        Returns:
            True if successful
        """
        cmd = ["scp"]
        
        # Options
        cmd.extend(["-P", str(self.ssh_config.port)])
        cmd.extend(["-o", "StrictHostKeyChecking=no"])
        cmd.extend(["-o", "UserKnownHostsFile=/dev/null"])
        
        if self.ssh_config.key_path:
            key_path = Path(self.ssh_config.key_path).expanduser()
            if key_path.exists():
                cmd.extend(["-i", str(key_path)])
        
        # Source and destination
        remote_spec = f"{self.ssh_config.user}@{self.ssh_config.host}:{remote_path}"
        
        if to_remote:
            cmd.extend([local_path, remote_spec])
            console.print(f"[cyan]Uploading {local_path} to {remote_path}...[/cyan]")
        else:
            cmd.extend([remote_spec, local_path])
            console.print(f"[cyan]Downloading {remote_path} to {local_path}...[/cyan]")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                console.print("[green]✓[/green] Transfer complete")
                return True
            else:
                console.print(f"[red]Transfer failed:[/red] {result.stderr}")
                return False
                
        except Exception as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            return False
    
    def run_command(self, command: str, capture: bool = False) -> Tuple[int, str, str]:
        """
        Run a command via SSH.
        
        Args:
            command: Command to run
            capture: Whether to capture output
            
        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        cmd = self._build_ssh_command(command=command)
        
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode, result.stdout, result.stderr
        else:
            result = subprocess.run(cmd)
            return result.returncode, "", ""


# Common port configurations
COMMON_PORTS = {
    "jupyter": PortForward(local_port=8888, remote_port=8888),
    "tensorboard": PortForward(local_port=6006, remote_port=6006),
    "vscode": PortForward(local_port=8080, remote_port=8080),
    "mlflow": PortForward(local_port=5000, remote_port=5000),
    "gradio": PortForward(local_port=7860, remote_port=7860),
    "streamlit": PortForward(local_port=8501, remote_port=8501),
}


def get_common_port(name: str) -> Optional[PortForward]:
    """Get common port forward by name."""
    return COMMON_PORTS.get(name.lower())


def parse_port_forwards(specs: List[str]) -> List[PortForward]:
    """
    Parse multiple port forward specifications.
    
    Args:
        specs: List of port specs (e.g., ["8888", "6006:6006", "jupyter"])
        
    Returns:
        List of PortForward objects
    """
    forwards = []
    
    for spec in specs:
        # Check if it's a common name
        common = get_common_port(spec)
        if common:
            forwards.append(common)
        else:
            forwards.append(PortForward.parse(spec))
    
    return forwards
