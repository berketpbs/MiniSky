"""
Rsync-based file synchronization for MiniSky.

Provides efficient file sync between local and remote VMs,
similar to SkyPilot's workdir sync functionality.

Features:
- Rsync-based sync for efficiency (only transfers changes)
- Fallback to SFTP when rsync is not available
- Exclude patterns support (.gitignore style)
- Progress reporting
- Bidirectional sync support
"""

import os
import subprocess
import shutil
import fnmatch
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging
import tempfile

import paramiko
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

logger = logging.getLogger(__name__)
console = Console()


class SyncDirection(str, Enum):
    """Direction of file sync."""
    LOCAL_TO_REMOTE = "upload"
    REMOTE_TO_LOCAL = "download"


@dataclass
class SyncConfig:
    """Configuration for file sync operations."""
    exclude_patterns: List[str] = field(default_factory=lambda: [
        ".git",
        ".git/**",
        "__pycache__",
        "__pycache__/**",
        "*.pyc",
        ".venv",
        ".venv/**",
        "venv",
        "venv/**",
        "node_modules",
        "node_modules/**",
        ".DS_Store",
        "*.egg-info",
        "*.egg-info/**",
        ".pytest_cache",
        ".pytest_cache/**",
        ".mypy_cache",
        ".mypy_cache/**",
        "*.log",
        ".env",
        ".env.*",
    ])
    delete_extraneous: bool = False  # Delete files on dest that don't exist on source
    compress: bool = True  # Compress during transfer
    preserve_times: bool = True
    preserve_permissions: bool = True
    dry_run: bool = False
    verbose: bool = False


@dataclass
class SyncResult:
    """Result of a sync operation."""
    success: bool
    files_transferred: int = 0
    bytes_transferred: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    method: str = "rsync"  # rsync or sftp


class RsyncSyncer:
    """
    Rsync-based file synchronization.
    
    Uses rsync over SSH for efficient incremental file transfers.
    Falls back to SFTP if rsync is not available.
    """
    
    def __init__(
        self,
        vm_info: Dict[str, Any],
        config: Optional[SyncConfig] = None,
    ):
        self.vm_info = vm_info
        self.config = config or SyncConfig()
        
        self._rsync_available: Optional[bool] = None
    
    def _check_rsync_available(self) -> bool:
        """Check if rsync is available locally."""
        if self._rsync_available is not None:
            return self._rsync_available
        
        try:
            result = subprocess.run(
                ["rsync", "--version"],
                capture_output=True,
                timeout=5
            )
            self._rsync_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._rsync_available = False
        
        return self._rsync_available
    
    def _build_rsync_command(
        self,
        local_path: str,
        remote_path: str,
        direction: SyncDirection,
    ) -> List[str]:
        """Build rsync command with all options."""
        hostname = self.vm_info['ip_address']
        port = self.vm_info.get('ssh_port', 22)
        username = self.vm_info.get('ssh_user', 'root')
        key_path = self.vm_info.get('ssh_key_path')
        
        # Base rsync command
        cmd = ["rsync", "-a"]  # Archive mode
        
        # Add options based on config
        if self.config.compress:
            cmd.append("-z")
        
        if self.config.verbose:
            cmd.append("-v")
        
        if self.config.delete_extraneous:
            cmd.append("--delete")
        
        if self.config.dry_run:
            cmd.append("--dry-run")
        
        # Progress
        cmd.append("--progress")
        
        # SSH options
        ssh_cmd = f"ssh -p {port}"
        if key_path:
            ssh_cmd += f" -i {key_path}"
        ssh_cmd += " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        cmd.extend(["-e", ssh_cmd])
        
        # Exclude patterns
        for pattern in self.config.exclude_patterns:
            cmd.extend(["--exclude", pattern])
        
        # Source and destination
        if direction == SyncDirection.LOCAL_TO_REMOTE:
            # Ensure local path ends with / to sync contents
            local = local_path.rstrip('/') + '/'
            remote = f"{username}@{hostname}:{remote_path}"
            cmd.extend([local, remote])
        else:
            remote = f"{username}@{hostname}:{remote_path.rstrip('/')}/"
            local = local_path.rstrip('/') + '/'
            cmd.extend([remote, local])
        
        return cmd
    
    def sync_rsync(
        self,
        local_path: str,
        remote_path: str,
        direction: SyncDirection = SyncDirection.LOCAL_TO_REMOTE,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> SyncResult:
        """
        Sync files using rsync.
        
        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            direction: Upload or download
            progress_callback: Optional callback for progress updates
        
        Returns:
            SyncResult with operation details
        """
        import time
        start_time = time.time()
        
        if not self._check_rsync_available():
            return SyncResult(
                success=False,
                error="rsync not available on local system",
                method="rsync"
            )
        
        cmd = self._build_rsync_command(local_path, remote_path, direction)
        
        logger.info(f"Running rsync: {' '.join(cmd)}")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            
            files_transferred = 0
            bytes_transferred = 0
            
            # Read output line by line
            for line in process.stdout:
                line = line.strip()
                if line:
                    if progress_callback:
                        progress_callback(line)
                    
                    # Parse rsync progress output
                    if not line.startswith(' ') and '/' not in line[:3]:
                        files_transferred += 1
            
            process.wait()
            
            if process.returncode != 0:
                stderr = process.stderr.read()
                return SyncResult(
                    success=False,
                    error=f"rsync failed: {stderr}",
                    method="rsync",
                    duration_seconds=time.time() - start_time
                )
            
            return SyncResult(
                success=True,
                files_transferred=files_transferred,
                bytes_transferred=bytes_transferred,
                duration_seconds=time.time() - start_time,
                method="rsync"
            )
            
        except Exception as e:
            return SyncResult(
                success=False,
                error=str(e),
                method="rsync",
                duration_seconds=time.time() - start_time
            )
    
    def sync_sftp(
        self,
        local_path: str,
        remote_path: str,
        direction: SyncDirection = SyncDirection.LOCAL_TO_REMOTE,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> SyncResult:
        """
        Sync files using SFTP (fallback when rsync not available).
        
        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            direction: Upload or download
            progress_callback: Callback(filename, bytes_so_far, total_bytes)
        
        Returns:
            SyncResult with operation details
        """
        import time
        start_time = time.time()
        
        ssh_client = None
        sftp_client = None
        
        try:
            # Connect
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            hostname = self.vm_info['ip_address']
            port = self.vm_info.get('ssh_port', 22)
            username = self.vm_info.get('ssh_user', 'root')
            key_path = self.vm_info.get('ssh_key_path')
            
            connect_kwargs = {
                'hostname': hostname,
                'port': port,
                'username': username,
                'timeout': 30,
            }
            
            if key_path:
                key_path_obj = Path(key_path)
                try:
                    key = paramiko.Ed25519Key.from_private_key_file(str(key_path_obj))
                except Exception:
                    try:
                        key = paramiko.RSAKey.from_private_key_file(str(key_path_obj))
                    except Exception:
                        key = None
                
                if key:
                    connect_kwargs['pkey'] = key
                else:
                    connect_kwargs['look_for_keys'] = True
            else:
                connect_kwargs['look_for_keys'] = True
            
            ssh_client.connect(**connect_kwargs)
            sftp_client = ssh_client.open_sftp()
            
            files_transferred = 0
            bytes_transferred = 0
            
            local_dir = Path(local_path)
            
            if direction == SyncDirection.LOCAL_TO_REMOTE:
                # Upload
                files_transferred, bytes_transferred = self._sftp_upload_dir(
                    sftp_client,
                    local_dir,
                    remote_path,
                    progress_callback
                )
            else:
                # Download
                files_transferred, bytes_transferred = self._sftp_download_dir(
                    sftp_client,
                    remote_path,
                    local_dir,
                    progress_callback
                )
            
            return SyncResult(
                success=True,
                files_transferred=files_transferred,
                bytes_transferred=bytes_transferred,
                duration_seconds=time.time() - start_time,
                method="sftp"
            )
            
        except Exception as e:
            return SyncResult(
                success=False,
                error=str(e),
                method="sftp",
                duration_seconds=time.time() - start_time
            )
        finally:
            if sftp_client:
                sftp_client.close()
            if ssh_client:
                ssh_client.close()
    
    def _should_exclude(self, path: Path, base_path: Path) -> bool:
        """Check if a path should be excluded based on patterns."""
        rel_path = str(path.relative_to(base_path))
        
        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(path.name, pattern):
                return True
        
        return False
    
    def _sftp_mkdir_p(self, sftp: paramiko.SFTPClient, remote_path: str):
        """Create remote directory recursively."""
        try:
            sftp.stat(remote_path)
        except FileNotFoundError:
            parent = str(Path(remote_path).parent)
            if parent != remote_path:
                self._sftp_mkdir_p(sftp, parent)
            sftp.mkdir(remote_path)
    
    def _sftp_upload_dir(
        self,
        sftp: paramiko.SFTPClient,
        local_dir: Path,
        remote_dir: str,
        progress_callback: Optional[Callable] = None,
    ) -> tuple:
        """Upload directory recursively via SFTP."""
        files_transferred = 0
        bytes_transferred = 0
        
        # Create remote directory
        self._sftp_mkdir_p(sftp, remote_dir)
        
        for item in local_dir.iterdir():
            if self._should_exclude(item, local_dir.parent):
                continue
            
            remote_path = f"{remote_dir}/{item.name}"
            
            if item.is_file():
                file_size = item.stat().st_size
                
                def _progress(transferred, total):
                    if progress_callback:
                        progress_callback(item.name, transferred, total)
                
                sftp.put(str(item), remote_path, callback=_progress)
                files_transferred += 1
                bytes_transferred += file_size
                
            elif item.is_dir():
                sub_files, sub_bytes = self._sftp_upload_dir(
                    sftp, item, remote_path, progress_callback
                )
                files_transferred += sub_files
                bytes_transferred += sub_bytes
        
        return files_transferred, bytes_transferred
    
    def _sftp_download_dir(
        self,
        sftp: paramiko.SFTPClient,
        remote_dir: str,
        local_dir: Path,
        progress_callback: Optional[Callable] = None,
    ) -> tuple:
        """Download directory recursively via SFTP."""
        files_transferred = 0
        bytes_transferred = 0
        
        # Create local directory
        local_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            items = sftp.listdir_attr(remote_dir)
        except Exception as e:
            logger.warning(f"Could not list {remote_dir}: {e}")
            return 0, 0
        
        for item in items:
            remote_path = f"{remote_dir}/{item.filename}"
            local_path = local_dir / item.filename
            
            # Check exclusions
            if any(fnmatch.fnmatch(item.filename, p) for p in self.config.exclude_patterns):
                continue
            
            if item.st_mode and (item.st_mode & 0o40000):  # Is directory
                sub_files, sub_bytes = self._sftp_download_dir(
                    sftp, remote_path, local_path, progress_callback
                )
                files_transferred += sub_files
                bytes_transferred += sub_bytes
            else:
                file_size = item.st_size or 0
                
                def _progress(transferred, total):
                    if progress_callback:
                        progress_callback(item.filename, transferred, total)
                
                sftp.get(remote_path, str(local_path), callback=_progress)
                files_transferred += 1
                bytes_transferred += file_size
        
        return files_transferred, bytes_transferred
    
    def sync(
        self,
        local_path: str,
        remote_path: str,
        direction: SyncDirection = SyncDirection.LOCAL_TO_REMOTE,
        prefer_rsync: bool = True,
    ) -> SyncResult:
        """
        Sync files between local and remote.
        
        Automatically chooses rsync if available, falls back to SFTP.
        
        Args:
            local_path: Local directory path
            remote_path: Remote directory path
            direction: Upload or download
            prefer_rsync: Try rsync first if available
        
        Returns:
            SyncResult with operation details
        """
        local_dir = Path(local_path).expanduser()
        
        if direction == SyncDirection.LOCAL_TO_REMOTE:
            if not local_dir.exists():
                return SyncResult(
                    success=False,
                    error=f"Local path does not exist: {local_path}"
                )
        
        # Try rsync first
        if prefer_rsync and self._check_rsync_available():
            console.print(f"[cyan]Using rsync for file sync...[/cyan]")
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                task = progress.add_task("Syncing files...", total=None)
                
                def on_progress(line: str):
                    progress.update(task, description=f"Syncing: {line[:50]}...")
                
                result = self.sync_rsync(
                    str(local_dir),
                    remote_path,
                    direction,
                    progress_callback=on_progress
                )
            
            if result.success:
                return result
            
            # Fall back to SFTP if rsync fails
            console.print(f"[yellow]rsync failed, falling back to SFTP...[/yellow]")
        
        # Use SFTP
        console.print(f"[cyan]Using SFTP for file sync...[/cyan]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Syncing files...", total=100)
            
            def on_progress(filename: str, transferred: int, total: int):
                if total > 0:
                    pct = (transferred / total) * 100
                    progress.update(task, completed=pct, description=f"Syncing: {filename}")
            
            result = self.sync_sftp(
                str(local_dir),
                remote_path,
                direction,
                progress_callback=on_progress
            )
        
        return result


def sync_workdir(
    vm_info: Dict[str, Any],
    local_path: str,
    remote_path: str = "~/workdir",
    exclude_patterns: Optional[List[str]] = None,
) -> SyncResult:
    """
    Convenience function to sync a workdir to a remote VM.
    
    Args:
        vm_info: VM connection details
        local_path: Local directory to sync
        remote_path: Remote destination path
        exclude_patterns: Additional patterns to exclude
    
    Returns:
        SyncResult with operation details
    """
    config = SyncConfig()
    if exclude_patterns:
        config.exclude_patterns.extend(exclude_patterns)
    
    syncer = RsyncSyncer(vm_info, config)
    
    console.print(f"\n[bold]Syncing workdir[/bold]")
    console.print(f"  Local:  {local_path}")
    console.print(f"  Remote: {remote_path}")
    
    result = syncer.sync(
        local_path,
        remote_path,
        SyncDirection.LOCAL_TO_REMOTE
    )
    
    if result.success:
        console.print(f"[green]✓[/green] Synced {result.files_transferred} files in {result.duration_seconds:.1f}s")
    else:
        console.print(f"[red]✗[/red] Sync failed: {result.error}")
    
    return result


def download_results(
    vm_info: Dict[str, Any],
    remote_path: str,
    local_path: str,
    exclude_patterns: Optional[List[str]] = None,
) -> SyncResult:
    """
    Download results from a remote VM.
    
    Args:
        vm_info: VM connection details
        remote_path: Remote directory to download
        local_path: Local destination path
        exclude_patterns: Patterns to exclude
    
    Returns:
        SyncResult with operation details
    """
    config = SyncConfig()
    if exclude_patterns:
        config.exclude_patterns.extend(exclude_patterns)
    
    syncer = RsyncSyncer(vm_info, config)
    
    console.print(f"\n[bold]Downloading results[/bold]")
    console.print(f"  Remote: {remote_path}")
    console.print(f"  Local:  {local_path}")
    
    result = syncer.sync(
        local_path,
        remote_path,
        SyncDirection.REMOTE_TO_LOCAL
    )
    
    if result.success:
        console.print(f"[green]✓[/green] Downloaded {result.files_transferred} files in {result.duration_seconds:.1f}s")
    else:
        console.print(f"[red]✗[/red] Download failed: {result.error}")
    
    return result
