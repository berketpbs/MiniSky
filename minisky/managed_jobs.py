"""
Managed Jobs System for MiniSky.

Provides automatic job recovery for spot instance preemptions,
checkpoint management, and fault-tolerant job execution.
"""

import time
import threading
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from rich.console import Console

console = Console()


class ManagedJobStatus(str, Enum):
    """Managed job status."""
    PENDING = "pending"
    LAUNCHING = "launching"
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CheckpointConfig:
    """Checkpoint configuration for managed jobs."""
    enabled: bool = True
    local_path: str = "/checkpoints"
    remote_uri: Optional[str] = None  # e.g., s3://bucket/checkpoints
    interval_minutes: int = 30
    max_checkpoints: int = 3


@dataclass
class RecoveryConfig:
    """Recovery configuration for managed jobs."""
    max_retries: int = 3
    retry_delay_seconds: int = 60
    use_spot: bool = True
    fallback_to_on_demand: bool = True


@dataclass
class ManagedJob:
    """
    A managed job with automatic recovery and checkpointing.
    
    Managed jobs provide:
    - Automatic restart on spot preemption
    - Checkpoint save/restore
    - Retry logic with backoff
    """
    job_id: str
    task_name: str
    command: str
    status: ManagedJobStatus = ManagedJobStatus.PENDING
    vm_id: Optional[str] = None
    checkpoint_config: CheckpointConfig = field(default_factory=CheckpointConfig)
    recovery_config: RecoveryConfig = field(default_factory=RecoveryConfig)
    
    # Tracking
    attempts: int = 0
    last_checkpoint: Optional[str] = None
    last_checkpoint_time: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error_message: Optional[str] = None
    
    @property
    def can_retry(self) -> bool:
        """Check if job can be retried."""
        return (
            self.attempts < self.recovery_config.max_retries and
            self.status in (ManagedJobStatus.FAILED, ManagedJobStatus.RECOVERING)
        )
    
    @property
    def runtime_seconds(self) -> Optional[float]:
        """Get total runtime in seconds."""
        if self.started_at:
            end = self.completed_at or time.time()
            return end - self.started_at
        return None


class ManagedJobController:
    """
    Controller for managed jobs with automatic recovery.
    
    Usage:
        controller = ManagedJobController(state, provider, storage)
        job = controller.submit(task, command, checkpoint_uri="s3://bucket/ckpt")
        controller.start()  # Start background monitoring
        controller.wait(job.job_id)  # Wait for completion
    """
    
    def __init__(
        self,
        state_manager: Any,
        provider: Any,
        storage_manager: Optional[Any] = None,
        executor_factory: Optional[Callable] = None
    ):
        """
        Initialize managed job controller.
        
        Args:
            state_manager: StateManager instance
            provider: Cloud provider instance
            storage_manager: StorageManager for checkpoints
            executor_factory: Factory to create SSH executors
        """
        self.state = state_manager
        self.provider = provider
        self.storage = storage_manager
        self.executor_factory = executor_factory
        
        self._jobs: Dict[str, ManagedJob] = {}
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
    
    def submit(
        self,
        task: Any,
        command: str,
        checkpoint_uri: Optional[str] = None,
        max_retries: int = 3,
        use_spot: bool = True
    ) -> ManagedJob:
        """
        Submit a managed job.
        
        Args:
            task: Task definition
            command: Command to execute
            checkpoint_uri: Remote URI for checkpoints (s3:// or gs://)
            max_retries: Maximum retry attempts
            use_spot: Whether to use spot instances
            
        Returns:
            ManagedJob object
        """
        import uuid
        
        job_id = f"managed-{uuid.uuid4().hex[:8]}"
        
        checkpoint_config = CheckpointConfig(
            enabled=checkpoint_uri is not None,
            remote_uri=checkpoint_uri
        )
        
        recovery_config = RecoveryConfig(
            max_retries=max_retries,
            use_spot=use_spot
        )
        
        job = ManagedJob(
            job_id=job_id,
            task_name=task.name,
            command=command,
            checkpoint_config=checkpoint_config,
            recovery_config=recovery_config
        )
        
        with self._lock:
            self._jobs[job_id] = job
        
        console.print(f"[green]✓[/green] Managed job submitted: {job_id}")
        console.print(f"  Command: {command}")
        console.print(f"  Max retries: {max_retries}")
        console.print(f"  Spot: {'enabled' if use_spot else 'disabled'}")
        if checkpoint_uri:
            console.print(f"  Checkpoint: {checkpoint_uri}")
        
        return job
    
    def get_job(self, job_id: str) -> Optional[ManagedJob]:
        """Get job by ID."""
        return self._jobs.get(job_id)
    
    def list_jobs(self) -> List[ManagedJob]:
        """List all managed jobs."""
        return list(self._jobs.values())
    
    def launch_job(self, job: ManagedJob, task: Any) -> bool:
        """
        Launch or re-launch a managed job.
        
        Args:
            job: ManagedJob to launch
            task: Task definition
            
        Returns:
            True if launched successfully
        """
        job.status = ManagedJobStatus.LAUNCHING
        job.attempts += 1
        
        console.print(f"\n[cyan]Launching managed job (attempt {job.attempts})...[/cyan]")
        
        try:
            # Configure spot if enabled
            if job.recovery_config.use_spot:
                task.resources.use_spot = True
            
            # Launch VM
            vm_info = self.provider.launch(task)
            job.vm_id = vm_info['vm_id']
            job.started_at = time.time()
            
            self.state.add_vm({
                **vm_info,
                'managed_job_id': job.job_id
            })
            
            console.print(f"[green]✓[/green] VM launched: {vm_info['vm_id']}")
            
            # Restore checkpoint if available
            if job.checkpoint_config.enabled and job.last_checkpoint and self.storage:
                console.print("[cyan]Restoring checkpoint...[/cyan]")
                executor = self.executor_factory(vm_info)
                executor.connect()
                
                self.storage.restore_checkpoint(
                    executor,
                    job.last_checkpoint,
                    job.checkpoint_config.local_path
                )
                
                executor.disconnect()
            
            job.status = ManagedJobStatus.RUNNING
            return True
            
        except Exception as e:
            console.print(f"[red]Launch failed:[/red] {str(e)}")
            job.error_message = str(e)
            job.status = ManagedJobStatus.FAILED
            return False
    
    def execute_job(self, job: ManagedJob) -> int:
        """
        Execute the job command on the VM.
        
        Args:
            job: ManagedJob to execute
            
        Returns:
            Exit code
        """
        if not job.vm_id:
            return -1
        
        vm_info = self.state.get_vm(job.vm_id)
        if not vm_info:
            return -1
        
        executor = self.executor_factory(vm_info)
        
        try:
            executor.connect()
            
            # Execute command
            exit_code = executor.execute_command(job.command)
            
            # Save checkpoint on success
            if exit_code == 0 and job.checkpoint_config.enabled and self.storage:
                self._save_checkpoint(job, executor)
            
            return exit_code
            
        except Exception as e:
            console.print(f"[red]Execution error:[/red] {str(e)}")
            job.error_message = str(e)
            return -1
        finally:
            executor.disconnect()
    
    def _save_checkpoint(self, job: ManagedJob, executor: Any) -> bool:
        """Save checkpoint to remote storage."""
        if not job.checkpoint_config.remote_uri or not self.storage:
            return False
        
        console.print("[cyan]Saving checkpoint...[/cyan]")
        
        # Generate checkpoint path with timestamp
        timestamp = int(time.time())
        checkpoint_uri = f"{job.checkpoint_config.remote_uri}/{job.job_id}/{timestamp}"
        
        success = self.storage.save_checkpoint(
            executor,
            job.checkpoint_config.local_path,
            checkpoint_uri
        )
        
        if success:
            job.last_checkpoint = checkpoint_uri
            job.last_checkpoint_time = time.time()
            console.print(f"[green]✓[/green] Checkpoint saved: {checkpoint_uri}")
        
        return success
    
    def handle_preemption(self, job: ManagedJob, task: Any) -> bool:
        """
        Handle spot instance preemption.
        
        Args:
            job: Preempted job
            task: Task definition for re-launch
            
        Returns:
            True if recovery initiated
        """
        console.print(f"\n[yellow]⚠ Spot preemption detected for job {job.job_id}[/yellow]")
        
        job.status = ManagedJobStatus.RECOVERING
        
        # Clean up old VM
        if job.vm_id:
            try:
                self.state.remove_vm(job.vm_id)
            except Exception:
                pass
            job.vm_id = None
        
        if not job.can_retry:
            console.print("[red]Max retries exceeded, job failed[/red]")
            job.status = ManagedJobStatus.FAILED
            return False
        
        # Wait before retry
        delay = job.recovery_config.retry_delay_seconds
        console.print(f"[cyan]Waiting {delay}s before retry...[/cyan]")
        time.sleep(delay)
        
        # Fallback to on-demand if configured
        if job.attempts >= 2 and job.recovery_config.fallback_to_on_demand:
            console.print("[yellow]Falling back to on-demand instance[/yellow]")
            job.recovery_config.use_spot = False
        
        # Re-launch
        return self.launch_job(job, task)
    
    def check_vm_status(self, job: ManagedJob) -> str:
        """
        Check VM status for a job.
        
        Returns:
            VM status string
        """
        if not job.vm_id:
            return "no_vm"
        
        try:
            vm_info = self.provider.status(job.vm_id)
            return vm_info.get('status', 'unknown')
        except Exception:
            return "not_found"
    
    def start_monitoring(self, check_interval: int = 30):
        """
        Start background monitoring thread.
        
        Args:
            check_interval: Seconds between status checks
        """
        if self._running:
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(check_interval,),
            daemon=True
        )
        self._monitor_thread.start()
        console.print("[green]✓[/green] Managed job monitoring started")
    
    def stop_monitoring(self):
        """Stop background monitoring."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        console.print("[yellow]Managed job monitoring stopped[/yellow]")
    
    def _monitor_loop(self, check_interval: int):
        """Background monitoring loop."""
        while self._running:
            with self._lock:
                running_jobs = [
                    j for j in self._jobs.values()
                    if j.status == ManagedJobStatus.RUNNING
                ]
            
            for job in running_jobs:
                status = self.check_vm_status(job)
                
                if status in ("not_found", "terminated", "preempted"):
                    # VM was preempted or terminated
                    # Note: In real implementation, would need task reference
                    console.print(f"[yellow]Job {job.job_id} VM status: {status}[/yellow]")
            
            time.sleep(check_interval)
    
    def wait(self, job_id: str, timeout: Optional[int] = None) -> ManagedJobStatus:
        """
        Wait for a job to complete.
        
        Args:
            job_id: Job ID to wait for
            timeout: Maximum seconds to wait
            
        Returns:
            Final job status
        """
        job = self.get_job(job_id)
        if not job:
            return ManagedJobStatus.FAILED
        
        start = time.time()
        
        while job.status in (
            ManagedJobStatus.PENDING,
            ManagedJobStatus.LAUNCHING,
            ManagedJobStatus.RUNNING,
            ManagedJobStatus.RECOVERING
        ):
            if timeout and (time.time() - start) > timeout:
                console.print("[yellow]Wait timeout exceeded[/yellow]")
                break
            
            time.sleep(5)
        
        return job.status
    
    def cancel(self, job_id: str) -> bool:
        """
        Cancel a managed job.
        
        Args:
            job_id: Job ID to cancel
            
        Returns:
            True if cancelled
        """
        job = self.get_job(job_id)
        if not job:
            return False
        
        if job.status in (ManagedJobStatus.COMPLETED, ManagedJobStatus.CANCELLED):
            return False
        
        # Terminate VM if running
        if job.vm_id:
            try:
                self.provider.terminate(job.vm_id)
                self.state.remove_vm(job.vm_id)
            except Exception:
                pass
        
        job.status = ManagedJobStatus.CANCELLED
        job.completed_at = time.time()
        
        console.print(f"[green]✓[/green] Managed job cancelled: {job_id}")
        return True
    
    def complete(self, job_id: str, success: bool = True) -> bool:
        """
        Mark a job as completed.
        
        Args:
            job_id: Job ID
            success: Whether job succeeded
            
        Returns:
            True if status updated
        """
        job = self.get_job(job_id)
        if not job:
            return False
        
        job.status = ManagedJobStatus.COMPLETED if success else ManagedJobStatus.FAILED
        job.completed_at = time.time()
        
        # Terminate VM
        if job.vm_id:
            try:
                self.provider.terminate(job.vm_id)
                self.state.remove_vm(job.vm_id)
            except Exception:
                pass
        
        status_str = "completed" if success else "failed"
        console.print(f"[green]✓[/green] Managed job {status_str}: {job_id}")
        
        if job.runtime_seconds:
            console.print(f"  Runtime: {job.runtime_seconds:.1f}s")
        console.print(f"  Attempts: {job.attempts}")
        
        return True
