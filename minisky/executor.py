"""
SSH executor for remote command execution.

This module handles SSH connections, file synchronization,
and command execution on remote VMs.
"""

import paramiko
import shlex
from pathlib import Path
from typing import Dict, Any, Optional, List
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


class ExecutorError(Exception):
    """Base exception for executor errors."""
    pass


class Executor:
    """
    Handles SSH connections and remote command execution.
    
    Features:
    - SSH connection management
    - File synchronization
    - Command execution with output streaming
    - Environment variable injection
    """
    
    def __init__(self, vm_info: Dict[str, Any]):
        """
        Initialize executor with VM information.
        
        Args:
            vm_info: VM information dictionary with connection details
        """
        self.vm_info = vm_info
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.sftp_client: Optional[paramiko.SFTPClient] = None
    
    def connect(self, timeout: int = 30, retries: int = 3) -> bool:
        """
        Establish SSH connection to VM.
        
        Args:
            timeout: Connection timeout in seconds
            retries: Number of connection attempts
            
        Returns:
            True if connected successfully
            
        Raises:
            ExecutorError: If connection fails after all retries
        """
        for attempt in range(retries):
            try:
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                # Get connection details
                hostname = self.vm_info['ip_address']
                port = self.vm_info.get('ssh_port', 22)
                username = self.vm_info.get('ssh_user', 'root')
                key_path = self.vm_info.get('ssh_key_path')
                
                # Connect
                if key_path:
                    key = paramiko.RSAKey.from_private_key_file(key_path)
                    self.ssh_client.connect(
                        hostname=hostname,
                        port=port,
                        username=username,
                        pkey=key,
                        timeout=timeout
                    )
                else:
                    # Try default key locations
                    self.ssh_client.connect(
                        hostname=hostname,
                        port=port,
                        username=username,
                        timeout=timeout,
                        look_for_keys=True
                    )
                
                # Open SFTP client for file operations
                self.sftp_client = self.ssh_client.open_sftp()
                
                console.print(f"[green]✓[/green] Connected to {hostname}")
                return True
                
            except Exception as e:
                if attempt < retries - 1:
                    console.print(f"[yellow]Connection attempt {attempt + 1} failed, retrying...[/yellow]")
                else:
                    raise ExecutorError(f"Failed to connect after {retries} attempts: {str(e)}")
        
        return False
    
    def disconnect(self):
        """Close SSH and SFTP connections."""
        if self.sftp_client:
            self.sftp_client.close()
        if self.ssh_client:
            self.ssh_client.close()
        console.print("[cyan]Disconnected from VM[/cyan]")
    
    def execute_command(
        self,
        command: str,
        env: Optional[Dict[str, str]] = None,
        stream_output: bool = True,
        workdir: Optional[str] = None
    ) -> int:
        """
        Execute a command on the remote VM.
        
        Args:
            command: Command to execute
            env: Environment variables
            stream_output: Whether to stream output to console
            workdir: Remote working directory to execute the command in
            
        Returns:
            Exit code of the command
            
        Raises:
            ExecutorError: If not connected or execution fails
        """
        if not self.ssh_client:
            raise ExecutorError("Not connected to VM")
        
        try:
            # Prepare environment
            env_str = ""
            if env:
                env_str = " ".join([f"{k}={shlex.quote(str(v))}" for k, v in env.items()]) + " "
            
            full_command = f"{env_str}{command}"
            
            # Change to workdir if specified
            if workdir:
                full_command = f"cd {workdir} && {full_command}"
            
            # Execute command
            stdin, stdout, stderr = self.ssh_client.exec_command(full_command)
            
            # Stream output
            if stream_output:
                for line in stdout:
                    console.print(line.rstrip())
                
                for line in stderr:
                    console.print(f"[red]{line.rstrip()}[/red]")
            
            # Get exit code
            exit_code = stdout.channel.recv_exit_status()
            
            return exit_code
            
        except Exception as e:
            raise ExecutorError(f"Command execution failed: {str(e)}")
    
    def sync_files(self, local_path: str, remote_path: str = "~/workdir"):
        """
        Sync local directory to remote VM.
        
        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            
        Raises:
            ExecutorError: If sync fails
        """
        if not self.sftp_client:
            raise ExecutorError("Not connected to VM")
        
        try:
            local_dir = Path(local_path).expanduser()
            
            if not local_dir.exists():
                raise ExecutorError(f"Local directory does not exist: {local_path}")
            
            # Normalize remote path if it starts with ~
            if remote_path.startswith('~/'):
                home_dir = self.sftp_client.normalize('.')
                remote_path = remote_path.replace('~', home_dir, 1)
            
            console.print(f"[cyan]Syncing files from {local_path} to {remote_path}...[/cyan]")
            
            # Create remote directory
            self._mkdir_p(remote_path)
            
            # Upload files recursively
            self._upload_dir(str(local_dir), remote_path)
            
            console.print(f"[green]✓[/green] Files synced successfully")
            
        except Exception as e:
            raise ExecutorError(f"File sync failed: {str(e)}")
    
    def _mkdir_p(self, remote_path: str):
        """Create remote directory recursively."""
        try:
            self.sftp_client.stat(remote_path)
        except FileNotFoundError:
            # Directory doesn't exist, create it
            parent = str(Path(remote_path).parent)
            if parent != remote_path:
                self._mkdir_p(parent)
            self.sftp_client.mkdir(remote_path)
    
    def _upload_dir(self, local_dir: str, remote_dir: str):
        """Upload directory recursively."""
        for item in Path(local_dir).iterdir():
            local_path = str(item)
            remote_path = f"{remote_dir}/{item.name}"
            
            if item.is_file():
                console.print(f"  Uploading {item.name}...")
                self.sftp_client.put(local_path, remote_path)
            elif item.is_dir():
                self._mkdir_p(remote_path)
                self._upload_dir(local_path, remote_path)
    
    def execute_task(self, task: Any):
        """
        Execute a complete task on the remote VM.
        
        Args:
            task: Task object with setup and run commands
            
        Raises:
            ExecutorError: If task execution fails
        """
        try:
            # Connect to VM
            self.connect()
            
            # Sync workdir if specified
            remote_workdir = "~/workdir" if task.workdir else None
            if task.workdir:
                self.sync_files(task.workdir, remote_path=remote_workdir)
            
            # Execute setup commands
            if task.setup:
                console.print("\n[bold cyan]Running setup commands...[/bold cyan]")
                for i, cmd in enumerate(task.setup, 1):
                    console.print(f"\n[cyan]Setup {i}/{len(task.setup)}:[/cyan] {cmd}")
                    exit_code = self.execute_command(cmd, env=task.env, workdir=remote_workdir)
                    if exit_code != 0:
                        raise ExecutorError(f"Setup command failed with exit code {exit_code}")
            
            # Execute run commands
            console.print("\n[bold green]Running main commands...[/bold green]")
            for i, cmd in enumerate(task.run, 1):
                console.print(f"\n[green]Run {i}/{len(task.run)}:[/green] {cmd}")
                exit_code = self.execute_command(cmd, env=task.env, workdir=remote_workdir)
                if exit_code != 0:
                    raise ExecutorError(f"Run command failed with exit code {exit_code}")
            
            console.print("\n[bold green]✓ Task completed successfully[/bold green]")
            
        finally:
            # Always disconnect
            self.disconnect()
