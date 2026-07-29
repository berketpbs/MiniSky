"""
Storage and File Mounts System for MiniSky.

Provides cloud storage integration (S3, GCS) with support for:
- COPY mode: Copy files to VM disk
- MOUNT mode: Mount bucket via FUSE
- Checkpoint save/restore
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from rich.console import Console

console = Console()


class MountMode(str, Enum):
    """File mount mode."""
    COPY = "copy"      # Copy files to VM disk
    MOUNT = "mount"    # Mount via FUSE (gcsfuse, s3fs)


class StorageProvider(str, Enum):
    """Supported storage providers."""
    S3 = "s3"
    GCS = "gcs"
    LOCAL = "local"


@dataclass
class FileMount:
    """
    Represents a file mount configuration.
    
    Example YAML:
        file_mounts:
          /data/dataset:
            source: s3://my-bucket/datasets/imagenet
            mode: copy
          /checkpoints:
            source: gs://my-bucket/checkpoints
            mode: mount
    """
    local_path: str           # Path on VM
    source: str               # Source URI (s3://, gs://, or local path)
    mode: MountMode = MountMode.COPY
    provider: Optional[StorageProvider] = None
    
    def __post_init__(self):
        """Detect provider from source URI."""
        if self.provider is None:
            if self.source.startswith("s3://"):
                self.provider = StorageProvider.S3
            elif self.source.startswith("gs://"):
                self.provider = StorageProvider.GCS
            else:
                self.provider = StorageProvider.LOCAL
    
    @property
    def bucket_name(self) -> Optional[str]:
        """Extract bucket name from URI."""
        if self.provider == StorageProvider.S3:
            return self.source.replace("s3://", "").split("/")[0]
        elif self.provider == StorageProvider.GCS:
            return self.source.replace("gs://", "").split("/")[0]
        return None
    
    @property
    def object_path(self) -> Optional[str]:
        """Extract object path from URI."""
        if self.provider in (StorageProvider.S3, StorageProvider.GCS):
            parts = self.source.split("/", 3)
            return parts[3] if len(parts) > 3 else ""
        return self.source


@dataclass
class StorageConfig:
    """Storage configuration from config file."""
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_region: str = "us-east-1"
    gcs_credentials_path: Optional[str] = None
    gcs_project: Optional[str] = None


class BaseStorageBackend(ABC):
    """Abstract base class for storage backends."""
    
    @abstractmethod
    def copy_to_vm(self, executor: Any, mount: FileMount) -> bool:
        """Copy files from storage to VM."""
        pass
    
    @abstractmethod
    def copy_from_vm(self, executor: Any, local_path: str, remote_uri: str) -> bool:
        """Copy files from VM to storage."""
        pass
    
    @abstractmethod
    def mount_on_vm(self, executor: Any, mount: FileMount) -> bool:
        """Mount storage on VM via FUSE."""
        pass
    
    @abstractmethod
    def unmount_on_vm(self, executor: Any, local_path: str) -> bool:
        """Unmount storage from VM."""
        pass


class S3Backend(BaseStorageBackend):
    """AWS S3 storage backend."""
    
    def __init__(self, config: StorageConfig):
        self.config = config
    
    def _get_env_vars(self) -> Dict[str, str]:
        """Get AWS environment variables."""
        env = {}
        if self.config.s3_access_key:
            env["AWS_ACCESS_KEY_ID"] = self.config.s3_access_key
        if self.config.s3_secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.config.s3_secret_key
        if self.config.s3_region:
            env["AWS_DEFAULT_REGION"] = self.config.s3_region
        return env
    
    def copy_to_vm(self, executor: Any, mount: FileMount) -> bool:
        """Copy files from S3 to VM using aws cli."""
        console.print(f"[cyan]Copying from {mount.source} to {mount.local_path}...[/cyan]")
        
        # Create directory
        executor.execute_command(f"mkdir -p {mount.local_path}", stream_output=False)
        
        # Use aws s3 sync
        cmd = f"aws s3 sync {mount.source} {mount.local_path}"
        exit_code = executor.execute_command(cmd, env=self._get_env_vars())
        
        return exit_code == 0
    
    def copy_from_vm(self, executor: Any, local_path: str, remote_uri: str) -> bool:
        """Copy files from VM to S3."""
        console.print(f"[cyan]Uploading from {local_path} to {remote_uri}...[/cyan]")
        
        cmd = f"aws s3 sync {local_path} {remote_uri}"
        exit_code = executor.execute_command(cmd, env=self._get_env_vars())
        
        return exit_code == 0
    
    def mount_on_vm(self, executor: Any, mount: FileMount) -> bool:
        """Mount S3 bucket using s3fs."""
        console.print(f"[cyan]Mounting {mount.source} at {mount.local_path}...[/cyan]")
        
        # Install s3fs if not present
        executor.execute_command(
            "which s3fs || (apt-get update && apt-get install -y s3fs)",
            stream_output=False
        )
        
        # Create mount point
        executor.execute_command(f"mkdir -p {mount.local_path}", stream_output=False)
        
        # Create credentials file
        if self.config.s3_access_key and self.config.s3_secret_key:
            creds = f"{self.config.s3_access_key}:{self.config.s3_secret_key}"
            executor.execute_command(
                f"echo '{creds}' > ~/.passwd-s3fs && chmod 600 ~/.passwd-s3fs",
                stream_output=False
            )
        
        # Mount
        bucket = mount.bucket_name
        obj_path = mount.object_path
        
        cmd = f"s3fs {bucket}:/{obj_path} {mount.local_path} -o passwd_file=~/.passwd-s3fs"
        exit_code = executor.execute_command(cmd)
        
        return exit_code == 0
    
    def unmount_on_vm(self, executor: Any, local_path: str) -> bool:
        """Unmount S3 bucket."""
        exit_code = executor.execute_command(f"fusermount -u {local_path}")
        return exit_code == 0


class GCSBackend(BaseStorageBackend):
    """Google Cloud Storage backend."""
    
    def __init__(self, config: StorageConfig):
        self.config = config
    
    def copy_to_vm(self, executor: Any, mount: FileMount) -> bool:
        """Copy files from GCS to VM using gsutil."""
        console.print(f"[cyan]Copying from {mount.source} to {mount.local_path}...[/cyan]")
        
        # Create directory
        executor.execute_command(f"mkdir -p {mount.local_path}", stream_output=False)
        
        # Use gsutil rsync
        cmd = f"gsutil -m rsync -r {mount.source} {mount.local_path}"
        exit_code = executor.execute_command(cmd)
        
        return exit_code == 0
    
    def copy_from_vm(self, executor: Any, local_path: str, remote_uri: str) -> bool:
        """Copy files from VM to GCS."""
        console.print(f"[cyan]Uploading from {local_path} to {remote_uri}...[/cyan]")
        
        cmd = f"gsutil -m rsync -r {local_path} {remote_uri}"
        exit_code = executor.execute_command(cmd)
        
        return exit_code == 0
    
    def mount_on_vm(self, executor: Any, mount: FileMount) -> bool:
        """Mount GCS bucket using gcsfuse."""
        console.print(f"[cyan]Mounting {mount.source} at {mount.local_path}...[/cyan]")
        
        # Install gcsfuse if not present
        install_cmd = """
        which gcsfuse || (
            export GCSFUSE_REPO=gcsfuse-$(lsb_release -c -s) &&
            echo "deb https://packages.cloud.google.com/apt $GCSFUSE_REPO main" | sudo tee /etc/apt/sources.list.d/gcsfuse.list &&
            curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add - &&
            sudo apt-get update &&
            sudo apt-get install -y gcsfuse
        )
        """
        executor.execute_command(install_cmd, stream_output=False)
        
        # Create mount point
        executor.execute_command(f"mkdir -p {mount.local_path}", stream_output=False)
        
        # Mount
        bucket = mount.bucket_name
        obj_path = mount.object_path
        
        if obj_path:
            cmd = f"gcsfuse --only-dir {obj_path} {bucket} {mount.local_path}"
        else:
            cmd = f"gcsfuse {bucket} {mount.local_path}"
        
        exit_code = executor.execute_command(cmd)
        
        return exit_code == 0
    
    def unmount_on_vm(self, executor: Any, local_path: str) -> bool:
        """Unmount GCS bucket."""
        exit_code = executor.execute_command(f"fusermount -u {local_path}")
        return exit_code == 0


class LocalBackend(BaseStorageBackend):
    """Local file system backend (uses SFTP)."""
    
    def copy_to_vm(self, executor: Any, mount: FileMount) -> bool:
        """Copy local files to VM using SFTP."""
        console.print(f"[cyan]Copying from {mount.source} to {mount.local_path}...[/cyan]")
        
        executor.sync_files(mount.source, mount.local_path)
        return True
    
    def copy_from_vm(self, executor: Any, local_path: str, remote_uri: str) -> bool:
        """Copy files from VM to local (not implemented)."""
        console.print("[yellow]Local download not yet implemented[/yellow]")
        return False
    
    def mount_on_vm(self, executor: Any, mount: FileMount) -> bool:
        """Local mount not supported, use copy instead."""
        console.print("[yellow]Local mount not supported, using copy mode[/yellow]")
        return self.copy_to_vm(executor, mount)
    
    def unmount_on_vm(self, executor: Any, local_path: str) -> bool:
        """No unmount needed for local files."""
        return True


class StorageManager:
    """
    Manages file mounts and cloud storage operations.
    
    Usage:
        storage = StorageManager(config)
        storage.setup_mounts(executor, file_mounts)
        storage.save_checkpoint(executor, "/checkpoints", "s3://bucket/run-1")
        storage.restore_checkpoint(executor, "s3://bucket/run-1", "/checkpoints")
    """
    
    def __init__(self, config: Optional[StorageConfig] = None):
        """Initialize storage manager."""
        self.config = config or StorageConfig()
        self._backends: Dict[StorageProvider, BaseStorageBackend] = {
            StorageProvider.S3: S3Backend(self.config),
            StorageProvider.GCS: GCSBackend(self.config),
            StorageProvider.LOCAL: LocalBackend(),
        }
    
    def get_backend(self, provider: StorageProvider) -> BaseStorageBackend:
        """Get storage backend for provider."""
        return self._backends[provider]
    
    def setup_mounts(self, executor: Any, mounts: List[FileMount]) -> Dict[str, bool]:
        """
        Setup all file mounts on VM.
        
        Args:
            executor: SSH executor
            mounts: List of FileMount configurations
            
        Returns:
            Dictionary mapping local_path to success status
        """
        results = {}
        
        for mount in mounts:
            backend = self.get_backend(mount.provider)
            
            try:
                if mount.mode == MountMode.COPY:
                    success = backend.copy_to_vm(executor, mount)
                else:
                    success = backend.mount_on_vm(executor, mount)
                
                results[mount.local_path] = success
                
                if success:
                    console.print(f"[green]✓[/green] Mounted {mount.source} at {mount.local_path}")
                else:
                    console.print(f"[red]✗[/red] Failed to mount {mount.source}")
                    
            except Exception as e:
                console.print(f"[red]Error mounting {mount.source}:[/red] {str(e)}")
                results[mount.local_path] = False
        
        return results
    
    def teardown_mounts(self, executor: Any, mounts: List[FileMount]) -> None:
        """Unmount all FUSE mounts."""
        for mount in mounts:
            if mount.mode == MountMode.MOUNT:
                backend = self.get_backend(mount.provider)
                backend.unmount_on_vm(executor, mount.local_path)
    
    def save_checkpoint(
        self,
        executor: Any,
        local_path: str,
        remote_uri: str,
        provider: Optional[StorageProvider] = None
    ) -> bool:
        """
        Save checkpoint from VM to cloud storage.
        
        Args:
            executor: SSH executor
            local_path: Path on VM
            remote_uri: Destination URI (s3:// or gs://)
            provider: Storage provider (auto-detected if None)
            
        Returns:
            True if successful
        """
        if provider is None:
            if remote_uri.startswith("s3://"):
                provider = StorageProvider.S3
            elif remote_uri.startswith("gs://"):
                provider = StorageProvider.GCS
            else:
                console.print(f"[red]Unknown storage provider for URI:[/red] {remote_uri}")
                return False
        
        console.print(f"[cyan]Saving checkpoint to {remote_uri}...[/cyan]")
        
        backend = self.get_backend(provider)
        success = backend.copy_from_vm(executor, local_path, remote_uri)
        
        if success:
            console.print(f"[green]✓[/green] Checkpoint saved to {remote_uri}")
        else:
            console.print(f"[red]✗[/red] Failed to save checkpoint")
        
        return success
    
    def restore_checkpoint(
        self,
        executor: Any,
        remote_uri: str,
        local_path: str,
        provider: Optional[StorageProvider] = None
    ) -> bool:
        """
        Restore checkpoint from cloud storage to VM.
        
        Args:
            executor: SSH executor
            remote_uri: Source URI (s3:// or gs://)
            local_path: Destination path on VM
            provider: Storage provider (auto-detected if None)
            
        Returns:
            True if successful
        """
        if provider is None:
            if remote_uri.startswith("s3://"):
                provider = StorageProvider.S3
            elif remote_uri.startswith("gs://"):
                provider = StorageProvider.GCS
            else:
                console.print(f"[red]Unknown storage provider for URI:[/red] {remote_uri}")
                return False
        
        console.print(f"[cyan]Restoring checkpoint from {remote_uri}...[/cyan]")
        
        mount = FileMount(
            local_path=local_path,
            source=remote_uri,
            mode=MountMode.COPY,
            provider=provider
        )
        
        backend = self.get_backend(provider)
        success = backend.copy_to_vm(executor, mount)
        
        if success:
            console.print(f"[green]✓[/green] Checkpoint restored to {local_path}")
        else:
            console.print(f"[red]✗[/red] Failed to restore checkpoint")
        
        return success


def parse_file_mounts(mounts_config: Dict[str, Any]) -> List[FileMount]:
    """
    Parse file_mounts from task YAML.
    
    Example YAML:
        file_mounts:
          /data/dataset:
            source: s3://my-bucket/datasets/imagenet
            mode: copy
          /checkpoints:
            source: gs://my-bucket/checkpoints
            mode: mount
          /code:
            source: ./local-code
            mode: copy
    
    Args:
        mounts_config: Dictionary from YAML
        
    Returns:
        List of FileMount objects
    """
    mounts = []
    
    for local_path, config in mounts_config.items():
        if isinstance(config, str):
            # Simple format: /path: s3://bucket/path
            mount = FileMount(local_path=local_path, source=config)
        else:
            # Full format with options
            mount = FileMount(
                local_path=local_path,
                source=config.get("source", config.get("src", "")),
                mode=MountMode(config.get("mode", "copy").lower())
            )
        
        mounts.append(mount)
    
    return mounts
