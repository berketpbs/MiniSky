"""
MiniSky Provisioner - Remote Machine Setup and Execution

This module handles the complete lifecycle of provisioning a remote machine:
1. Wait for SSH to become available
2. Inject SSH keys if needed
3. Run setup commands (install dependencies, configure environment)
4. Run the main task command
5. Stream logs back to the user

This is the critical "glue" between launching a VM and actually running code on it.
Similar to SkyPilot's sky.backends.cloud_vm_ray_backend
"""

import os
import time
import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging
import socket

from minisky.executor import Executor, ExecutorError
from minisky.state import StateManager

logger = logging.getLogger(__name__)


class ProvisionState(str, Enum):
    """Provisioning state machine."""
    PENDING = "pending"
    WAITING_SSH = "waiting_ssh"
    INJECTING_KEYS = "injecting_keys"
    RUNNING_SETUP = "running_setup"
    RUNNING_TASK = "running_task"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ProvisionResult:
    """Result of provisioning operation."""
    success: bool
    state: ProvisionState
    vm_id: str
    exit_code: Optional[int] = None
    output: str = ""
    error: str = ""
    duration_seconds: float = 0.0


@dataclass
class ProvisionConfig:
    """Configuration for provisioning."""
    ssh_timeout: int = 300  # Max seconds to wait for SSH
    ssh_retry_interval: int = 5  # Seconds between SSH attempts
    setup_timeout: int = 600  # Max seconds for setup commands
    run_timeout: int = 0  # 0 = no timeout for run command
    stream_logs: bool = True
    log_callback: Optional[Callable[[str], None]] = None


class SSHKeyManager:
    """
    Manages SSH keys for remote access.
    
    Handles:
    - Finding existing SSH keys
    - Generating new keys if needed
    - Injecting public keys into remote machines
    """
    
    def __init__(self):
        self._key_path: Optional[Path] = None
    
    def get_key_path(self) -> Path:
        """Get path to SSH private key, creating one if needed."""
        if self._key_path and self._key_path.exists():
            return self._key_path
        
        # Check default locations
        default_paths = [
            Path.home() / ".ssh" / "id_ed25519",
            Path.home() / ".ssh" / "id_rsa",
            Path.home() / ".minisky" / "ssh" / "id_ed25519",
        ]
        
        for path in default_paths:
            if path.exists():
                self._key_path = path
                return path
        
        # Generate new key
        return self._generate_key()
    
    def _generate_key(self) -> Path:
        """Generate a new SSH key pair for MiniSky."""
        ssh_dir = Path.home() / ".minisky" / "ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        
        key_path = ssh_dir / "id_ed25519"
        pub_path = ssh_dir / "id_ed25519.pub"
        
        if key_path.exists():
            self._key_path = key_path
            return key_path
        
        logger.info(f"Generating new SSH key at {key_path}")
        
        try:
            subprocess.run([
                "ssh-keygen",
                "-t", "ed25519",
                "-f", str(key_path),
                "-N", "",  # No passphrase
                "-C", "minisky-generated"
            ], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to generate SSH key: {e.stderr.decode()}")
        except FileNotFoundError:
            raise RuntimeError("ssh-keygen not found. Please install OpenSSH.")
        
        # Set correct permissions
        key_path.chmod(0o600)
        pub_path.chmod(0o644)
        
        self._key_path = key_path
        return key_path
    
    def get_public_key(self) -> str:
        """Get the public key content."""
        key_path = self.get_key_path()
        pub_path = key_path.with_suffix(".pub")
        
        if not pub_path.exists():
            # Try to derive from private key
            try:
                result = subprocess.run(
                    ["ssh-keygen", "-y", "-f", str(key_path)],
                    capture_output=True,
                    check=True
                )
                return result.stdout.decode().strip()
            except subprocess.CalledProcessError:
                raise RuntimeError(f"Cannot find or derive public key for {key_path}")
        
        return pub_path.read_text().strip()


class Provisioner:
    """
    Handles the complete provisioning lifecycle for a remote machine.
    
    This is the core component that makes MiniSky actually work -
    it bridges the gap between "VM exists" and "code is running".
    """
    
    def __init__(
        self,
        vm_info: Dict[str, Any],
        config: Optional[ProvisionConfig] = None
    ):
        self.vm_info = vm_info
        self.config = config or ProvisionConfig()
        self.state = ProvisionState.PENDING
        self.ssh_key_manager = SSHKeyManager()
        self._executor: Optional[Executor] = None
        self._start_time: float = 0
    
    def _create_executor(self) -> Executor:
        """Create an executor with SSH key path."""
        key_path = self.ssh_key_manager.get_key_path()
        
        # Add key path to vm_info
        vm_info_with_key = dict(self.vm_info)
        vm_info_with_key["ssh_key_path"] = str(key_path)
        
        return Executor(vm_info_with_key)
    
    @property
    def executor(self) -> Executor:
        """Get or create SSH executor."""
        if self._executor is None:
            self._executor = self._create_executor()
        return self._executor
    
    def _log(self, message: str):
        """Log a message and optionally call the callback."""
        logger.info(message)
        if self.config.log_callback:
            self.config.log_callback(message)
    
    def _transition(self, new_state: ProvisionState, reason: str = ""):
        """Transition to a new state."""
        old_state = self.state
        self.state = new_state
        self._log(f"[{self.vm_info.get('vm_id', 'unknown')}] {old_state} -> {new_state}" + (f": {reason}" if reason else ""))
    
    def wait_for_ssh(self) -> bool:
        """
        Wait for SSH to become available on the remote machine.
        
        Returns:
            True if SSH is available, False if timeout
        """
        self._transition(ProvisionState.WAITING_SSH)
        
        host = self.vm_info["ip_address"]
        port = self.vm_info.get("ssh_port", 22)
        
        start_time = time.time()
        attempt = 0
        
        while time.time() - start_time < self.config.ssh_timeout:
            attempt += 1
            
            # First check if port is open
            if self._check_port(host, port):
                # Try actual SSH connection
                try:
                    self.executor.connect()
                    self._log(f"SSH connection established after {attempt} attempts")
                    return True
                except ExecutorError as e:
                    self._log(f"SSH attempt {attempt} failed: {e}")
                except Exception as e:
                    self._log(f"SSH attempt {attempt} failed: {e}")
            else:
                self._log(f"Port {port} not open yet (attempt {attempt})")
            
            time.sleep(self.config.ssh_retry_interval)
        
        self._transition(ProvisionState.FAILED, "SSH timeout")
        return False
    
    def _check_port(self, host: str, port: int, timeout: float = 5.0) -> bool:
        """Check if a port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False
    
    def inject_ssh_key(self) -> bool:
        """
        Inject SSH public key into the remote machine's authorized_keys.
        
        Some providers (like RunPod) handle this automatically,
        but we provide this for providers that don't.
        
        Returns:
            True if successful
        """
        self._transition(ProvisionState.INJECTING_KEYS)
        
        try:
            public_key = self.ssh_key_manager.get_public_key()
            
            # Ensure .ssh directory exists with correct permissions
            commands = [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                f"echo '{public_key}' >> ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
                "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",  # Remove duplicates
            ]
            
            for cmd in commands:
                exit_code = self.executor.execute_command(cmd, stream_output=False)
                if exit_code != 0:
                    self._log(f"Warning: Command failed: {cmd}")
            
            return True
            
        except Exception as e:
            self._log(f"SSH key injection failed: {e}")
            return False
    
    def run_setup(self, setup_commands: List[str]) -> Tuple[bool, str]:
        """
        Run setup commands on the remote machine.
        
        Args:
            setup_commands: List of shell commands to run
        
        Returns:
            Tuple of (success, output)
        """
        if not setup_commands:
            return True, ""
        
        self._transition(ProvisionState.RUNNING_SETUP)
        
        output_lines: List[str] = []
        
        for cmd in setup_commands:
            self._log(f"[setup] Running: {cmd}")
            
            try:
                exit_code = self.executor.execute_command(
                    cmd,
                    stream_output=self.config.stream_logs
                )
                
                if exit_code != 0:
                    self._transition(ProvisionState.FAILED, f"Setup command failed: {cmd}")
                    return False, "\n".join(output_lines)
                    
            except Exception as e:
                self._transition(ProvisionState.FAILED, f"Setup error: {e}")
                return False, str(e)
        
        return True, "\n".join(output_lines)
    
    def run_task(self, run_command: str, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        """
        Run the main task command on the remote machine.
        
        Args:
            run_command: The command to execute
            env: Environment variables to set
        
        Returns:
            Tuple of (exit_code, output)
        """
        self._transition(ProvisionState.RUNNING_TASK)
        
        # Build command with environment variables
        if env:
            env_exports = " ".join([f"{k}={v}" for k, v in env.items()])
            full_command = f"export {env_exports} && {run_command}"
        else:
            full_command = run_command
        
        self._log(f"[run] Executing: {run_command}")
        
        try:
            exit_code = self.executor.execute_command(
                full_command,
                stream_output=self.config.stream_logs
            )
            
            if exit_code == 0:
                self._transition(ProvisionState.COMPLETED)
            else:
                self._transition(ProvisionState.FAILED, f"Task exited with code {exit_code}")
            
            return exit_code, ""
            
        except Exception as e:
            self._transition(ProvisionState.FAILED, f"Task error: {e}")
            return -1, str(e)
    
    def provision_and_run(
        self,
        setup_commands: Optional[List[str]] = None,
        run_command: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        inject_key: bool = False
    ) -> ProvisionResult:
        """
        Complete provisioning lifecycle: wait for SSH, setup, run.
        
        Args:
            setup_commands: Commands to run during setup phase
            run_command: Main command to execute
            env: Environment variables
            inject_key: Whether to inject SSH key
        
        Returns:
            ProvisionResult with outcome details
        """
        self._start_time = time.time()
        vm_id = self.vm_info.get("vm_id", "unknown")
        
        try:
            # Step 1: Wait for SSH
            if not self.wait_for_ssh():
                return ProvisionResult(
                    success=False,
                    state=self.state,
                    vm_id=vm_id,
                    error="SSH connection timeout"
                )
            
            # Step 2: Inject SSH key if requested
            if inject_key:
                self.inject_ssh_key()
            
            # Step 3: Run setup commands
            if setup_commands:
                success, setup_output = self.run_setup(setup_commands)
                if not success:
                    return ProvisionResult(
                        success=False,
                        state=self.state,
                        vm_id=vm_id,
                        output=setup_output,
                        error="Setup failed",
                        duration_seconds=time.time() - self._start_time
                    )
            
            # Step 4: Run main task
            if run_command:
                exit_code, run_output = self.run_task(run_command, env)
                return ProvisionResult(
                    success=(exit_code == 0),
                    state=self.state,
                    vm_id=vm_id,
                    exit_code=exit_code,
                    output=run_output,
                    duration_seconds=time.time() - self._start_time
                )
            
            # No run command - just setup
            self._transition(ProvisionState.COMPLETED)
            return ProvisionResult(
                success=True,
                state=self.state,
                vm_id=vm_id,
                duration_seconds=time.time() - self._start_time
            )
            
        except Exception as e:
            self._transition(ProvisionState.FAILED, str(e))
            return ProvisionResult(
                success=False,
                state=self.state,
                vm_id=vm_id,
                error=str(e),
                duration_seconds=time.time() - self._start_time
            )
        finally:
            if self._executor:
                try:
                    self._executor.disconnect()
                except Exception:
                    pass


class AsyncProvisioner:
    """
    Async version of Provisioner for use with asyncio.
    
    Wraps the sync Provisioner in async methods for integration
    with the async API server.
    """
    
    def __init__(
        self,
        vm_info: Dict[str, Any],
        config: Optional[ProvisionConfig] = None
    ):
        self.vm_info = vm_info
        self.config = config or ProvisionConfig()
        self._provisioner: Optional[Provisioner] = None
    
    async def provision_and_run(
        self,
        setup_commands: Optional[List[str]] = None,
        run_command: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        inject_key: bool = False
    ) -> ProvisionResult:
        """Async wrapper for provision_and_run."""
        loop = asyncio.get_event_loop()
        
        self._provisioner = Provisioner(self.vm_info, self.config)
        
        # Run in thread pool to avoid blocking
        result = await loop.run_in_executor(
            None,
            lambda: self._provisioner.provision_and_run(
                setup_commands=setup_commands,
                run_command=run_command,
                env=env,
                inject_key=inject_key
            )
        )
        
        return result
    
    @property
    def state(self) -> ProvisionState:
        """Get current provisioning state."""
        if self._provisioner:
            return self._provisioner.state
        return ProvisionState.PENDING


def provision_vm(
    vm_info: Dict[str, Any],
    setup_commands: Optional[List[str]] = None,
    run_command: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    stream_logs: bool = True,
    log_callback: Optional[Callable[[str], None]] = None
) -> ProvisionResult:
    """
    Convenience function to provision a VM and run a task.
    
    Args:
        vm_info: VM information dict with ip_address, ssh_port, ssh_user
        setup_commands: Optional setup commands
        run_command: Optional main command
        env: Environment variables
        stream_logs: Whether to stream logs
        log_callback: Optional callback for log lines
    
    Returns:
        ProvisionResult
    
    Example:
        result = provision_vm(
            vm_info={"ip_address": "1.2.3.4", "ssh_port": 22, "ssh_user": "root"},
            setup_commands=["pip install torch"],
            run_command="python train.py",
            env={"CUDA_VISIBLE_DEVICES": "0"}
        )
        if result.success:
            print(f"Task completed with exit code {result.exit_code}")
    """
    config = ProvisionConfig(
        stream_logs=stream_logs,
        log_callback=log_callback
    )
    
    provisioner = Provisioner(vm_info, config)
    return provisioner.provision_and_run(
        setup_commands=setup_commands,
        run_command=run_command,
        env=env
    )
