"""
Enhanced autostop agent for MiniSky.

Provides intelligent idle detection by monitoring actual
resource usage on remote VMs via SSH:
- CPU utilization monitoring
- GPU utilization monitoring (nvidia-smi)
- Network activity detection
- Process monitoring

This is a critical cost-saving feature that prevents
VMs from running indefinitely when not in use.
"""

import time
import threading
import logging
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

import paramiko
from rich.console import Console

from .config import MiniSkyConfig
from .state import StateManager
from .providers import get_provider

logger = logging.getLogger(__name__)
console = Console()


class IdleReason(str, Enum):
    """Reason for considering a VM idle."""
    CPU_IDLE = "cpu_idle"
    GPU_IDLE = "gpu_idle"
    NO_PROCESSES = "no_processes"
    TIMEOUT = "timeout"
    MANUAL = "manual"


@dataclass
class ResourceMetrics:
    """Resource utilization metrics from a VM."""
    cpu_percent: float = 0.0
    gpu_percent: float = 0.0
    gpu_memory_percent: float = 0.0
    memory_percent: float = 0.0
    network_rx_bytes: int = 0
    network_tx_bytes: int = 0
    active_processes: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def is_idle(self) -> bool:
        """Check if metrics indicate idle state."""
        # Consider idle if CPU < 5% and GPU < 5%
        return self.cpu_percent < 5.0 and self.gpu_percent < 5.0


@dataclass
class AutostopConfig:
    """Configuration for autostop behavior."""
    idle_timeout_minutes: int = 30
    check_interval_seconds: int = 60
    cpu_idle_threshold: float = 5.0  # Percent
    gpu_idle_threshold: float = 5.0  # Percent
    require_consecutive_idle: int = 3  # Number of consecutive idle checks
    monitor_gpu: bool = True
    monitor_network: bool = False
    stop_on_idle: bool = True  # Stop vs terminate
    notify_before_stop: bool = True
    notify_minutes_before: int = 5


class ResourceMonitor:
    """
    Monitors resource usage on a remote VM via SSH.
    
    Collects CPU, GPU, memory, and network metrics to
    determine if the VM is idle.
    """
    
    def __init__(self, vm_info: Dict[str, Any]):
        self.vm_info = vm_info
        self._ssh_client: Optional[paramiko.SSHClient] = None
    
    def _connect(self) -> bool:
        """Establish SSH connection to VM."""
        try:
            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            hostname = self.vm_info['ip_address']
            port = self.vm_info.get('ssh_port', 22)
            username = self.vm_info.get('ssh_user', 'root')
            key_path = self.vm_info.get('ssh_key_path')
            
            connect_kwargs = {
                'hostname': hostname,
                'port': port,
                'username': username,
                'timeout': 10,
            }
            
            if key_path:
                connect_kwargs['key_filename'] = key_path
            else:
                connect_kwargs['look_for_keys'] = True
            
            self._ssh_client.connect(**connect_kwargs)
            return True
            
        except Exception as e:
            logger.warning(f"Failed to connect to VM: {e}")
            return False
    
    def _disconnect(self):
        """Close SSH connection."""
        if self._ssh_client:
            self._ssh_client.close()
            self._ssh_client = None
    
    def _exec_command(self, cmd: str) -> str:
        """Execute command and return stdout."""
        if not self._ssh_client:
            return ""
        
        try:
            stdin, stdout, stderr = self._ssh_client.exec_command(cmd, timeout=10)
            return stdout.read().decode('utf-8', errors='replace').strip()
        except Exception as e:
            logger.warning(f"Command failed: {cmd}: {e}")
            return ""
    
    def get_cpu_usage(self) -> float:
        """Get CPU usage percentage."""
        # Use top in batch mode for single snapshot
        output = self._exec_command(
            "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1"
        )
        
        if not output:
            # Fallback: use /proc/stat
            output = self._exec_command(
                "cat /proc/stat | grep '^cpu ' | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'"
            )
        
        try:
            return float(output)
        except (ValueError, TypeError):
            return 0.0
    
    def get_gpu_usage(self) -> tuple:
        """
        Get GPU usage percentage and memory usage.
        
        Returns:
            Tuple of (gpu_percent, memory_percent)
        """
        # Try nvidia-smi
        output = self._exec_command(
            "nvidia-smi --query-gpu=utilization.gpu,utilization.memory "
            "--format=csv,noheader,nounits 2>/dev/null | head -1"
        )
        
        if output:
            try:
                parts = output.split(',')
                gpu_util = float(parts[0].strip())
                mem_util = float(parts[1].strip()) if len(parts) > 1 else 0.0
                return gpu_util, mem_util
            except (ValueError, IndexError):
                pass
        
        return 0.0, 0.0
    
    def get_memory_usage(self) -> float:
        """Get memory usage percentage."""
        output = self._exec_command(
            "free | grep Mem | awk '{print $3/$2 * 100.0}'"
        )
        
        try:
            return float(output)
        except (ValueError, TypeError):
            return 0.0
    
    def get_active_processes(self) -> int:
        """Get count of user processes (excluding system processes)."""
        output = self._exec_command(
            "ps aux --no-headers | grep -v '\\[' | wc -l"
        )
        
        try:
            return int(output)
        except (ValueError, TypeError):
            return 0
    
    def get_network_activity(self) -> tuple:
        """
        Get network RX/TX bytes.
        
        Returns:
            Tuple of (rx_bytes, tx_bytes)
        """
        output = self._exec_command(
            "cat /proc/net/dev | grep -E 'eth0|ens' | head -1 | "
            "awk '{print $2, $10}'"
        )
        
        if output:
            try:
                parts = output.split()
                rx = int(parts[0])
                tx = int(parts[1]) if len(parts) > 1 else 0
                return rx, tx
            except (ValueError, IndexError):
                pass
        
        return 0, 0
    
    def collect_metrics(self) -> Optional[ResourceMetrics]:
        """
        Collect all resource metrics from the VM.
        
        Returns:
            ResourceMetrics or None if collection failed
        """
        if not self._connect():
            return None
        
        try:
            cpu = self.get_cpu_usage()
            gpu, gpu_mem = self.get_gpu_usage()
            memory = self.get_memory_usage()
            processes = self.get_active_processes()
            rx, tx = self.get_network_activity()
            
            return ResourceMetrics(
                cpu_percent=cpu,
                gpu_percent=gpu,
                gpu_memory_percent=gpu_mem,
                memory_percent=memory,
                network_rx_bytes=rx,
                network_tx_bytes=tx,
                active_processes=processes,
            )
            
        finally:
            self._disconnect()


class AutostopAgent:
    """
    Enhanced autostop agent with real resource monitoring.
    
    Monitors VMs via SSH to detect actual idle state based on
    CPU/GPU utilization rather than just timestamps.
    """
    
    def __init__(
        self,
        config: Optional[MiniSkyConfig] = None,
        state: Optional[StateManager] = None,
        autostop_config: Optional[AutostopConfig] = None,
    ):
        self._config = config or MiniSkyConfig()
        self._state = state or StateManager()
        self._autostop_config = autostop_config or AutostopConfig()
        
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        # Tracked VMs: vm_id -> tracking info
        self._tracked_vms: Dict[str, Dict[str, Any]] = {}
        
        # Callbacks
        self._on_idle_callback: Optional[Callable] = None
        self._on_stop_callback: Optional[Callable] = None
    
    def register(
        self,
        vm_id: str,
        timeout_minutes: Optional[int] = None,
        config: Optional[AutostopConfig] = None,
    ):
        """
        Register a VM for autostop monitoring.
        
        Args:
            vm_id: VM identifier
            timeout_minutes: Override idle timeout
            config: Override autostop config for this VM
        """
        vm_config = config or self._autostop_config
        if timeout_minutes:
            vm_config = AutostopConfig(
                idle_timeout_minutes=timeout_minutes,
                **{k: v for k, v in vars(vm_config).items() if k != 'idle_timeout_minutes'}
            )
        
        self._tracked_vms[vm_id] = {
            'config': vm_config,
            'consecutive_idle': 0,
            'last_active': datetime.now(),
            'metrics_history': [],
            'warned': False,
        }
        
        logger.info(f"Registered VM {vm_id} for autostop (timeout: {vm_config.idle_timeout_minutes}m)")
    
    def unregister(self, vm_id: str):
        """Remove a VM from autostop monitoring."""
        self._tracked_vms.pop(vm_id, None)
        logger.info(f"Unregistered VM {vm_id} from autostop")
    
    def set_callbacks(
        self,
        on_idle: Optional[Callable[[str, IdleReason], None]] = None,
        on_stop: Optional[Callable[[str], None]] = None,
    ):
        """Set callback functions for idle detection and stop events."""
        self._on_idle_callback = on_idle
        self._on_stop_callback = on_stop
    
    def start(self):
        """Start the autostop monitoring daemon."""
        if self._thread and self._thread.is_alive():
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="autostop-agent",
        )
        self._thread.start()
        logger.info("Autostop agent started")
    
    def stop(self):
        """Stop the autostop monitoring daemon."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # Still running (e.g. mid-sleep past the timeout) - leave
                # _thread set so start() sees is_alive()==True and refuses
                # to spawn a second, concurrent monitor loop.
                logger.warning("Autostop agent thread did not stop within timeout")
            else:
                self._thread = None
        logger.info("Autostop agent stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                self._check_all_vms()
            except Exception as e:
                logger.error(f"Autostop monitor error: {e}")
            
            time.sleep(self._autostop_config.check_interval_seconds)
    
    def _check_all_vms(self):
        """Check all registered VMs for idle state."""
        for vm_id in list(self._tracked_vms.keys()):
            try:
                self._check_vm(vm_id)
            except Exception as e:
                logger.warning(f"Error checking VM {vm_id}: {e}")
    
    def _check_vm(self, vm_id: str):
        """Check a single VM for idle state."""
        tracking = self._tracked_vms.get(vm_id)
        if not tracking:
            return
        
        vm_info = self._state.get_vm(vm_id)
        if not vm_info:
            self.unregister(vm_id)
            return
        
        if vm_info['status'] != 'running':
            return
        
        config = tracking['config']
        
        # Collect metrics
        monitor = ResourceMonitor(vm_info)
        metrics = monitor.collect_metrics()
        
        if metrics:
            # Store in history
            tracking['metrics_history'].append(metrics)
            if len(tracking['metrics_history']) > 10:
                tracking['metrics_history'] = tracking['metrics_history'][-10:]
            
            # Check if idle
            is_idle = self._is_vm_idle(metrics, config)
            
            if is_idle:
                tracking['consecutive_idle'] += 1
                logger.debug(f"VM {vm_id} idle check {tracking['consecutive_idle']}/{config.require_consecutive_idle}")
            else:
                tracking['consecutive_idle'] = 0
                tracking['last_active'] = datetime.now()
                tracking['warned'] = False
            
            # Check if should stop
            if tracking['consecutive_idle'] >= config.require_consecutive_idle:
                idle_duration = datetime.now() - tracking['last_active']
                idle_minutes = idle_duration.total_seconds() / 60
                
                if idle_minutes >= config.idle_timeout_minutes:
                    self._handle_idle_vm(vm_id, vm_info, IdleReason.CPU_IDLE if metrics.cpu_percent < config.cpu_idle_threshold else IdleReason.GPU_IDLE)
                elif config.notify_before_stop and not tracking['warned']:
                    remaining = config.idle_timeout_minutes - idle_minutes
                    if remaining <= config.notify_minutes_before:
                        self._warn_idle_vm(vm_id, remaining)
                        tracking['warned'] = True
        else:
            # Couldn't collect metrics, fall back to timestamp-based check
            logger.warning(f"Could not collect metrics for {vm_id}, using timestamp fallback")
            self._check_timestamp_idle(vm_id, vm_info, tracking)
    
    def _is_vm_idle(self, metrics: ResourceMetrics, config: AutostopConfig) -> bool:
        """Determine if metrics indicate idle state."""
        cpu_idle = metrics.cpu_percent < config.cpu_idle_threshold
        
        if config.monitor_gpu:
            gpu_idle = metrics.gpu_percent < config.gpu_idle_threshold
            return cpu_idle and gpu_idle
        
        return cpu_idle
    
    def _check_timestamp_idle(self, vm_id: str, vm_info: dict, tracking: dict):
        """Fallback to timestamp-based idle check."""
        config = tracking['config']
        updated_at = vm_info.get('updated_at', '')
        
        if not updated_at:
            return
        
        try:
            last_update = datetime.fromisoformat(str(updated_at))
            idle_duration = datetime.now() - last_update
            
            if idle_duration > timedelta(minutes=config.idle_timeout_minutes):
                self._handle_idle_vm(vm_id, vm_info, IdleReason.TIMEOUT)
        except (ValueError, TypeError):
            pass
    
    def _warn_idle_vm(self, vm_id: str, minutes_remaining: float):
        """Warn that a VM will be stopped soon."""
        console.print(
            f"[yellow]⚠️  Autostop warning:[/yellow] VM {vm_id} will be stopped "
            f"in {minutes_remaining:.0f} minutes due to inactivity"
        )
    
    def _handle_idle_vm(self, vm_id: str, vm_info: dict, reason: IdleReason):
        """Handle an idle VM by stopping or terminating it."""
        config = self._tracked_vms[vm_id]['config']
        provider_name = vm_info.get('provider', 'unknown')
        
        console.print(
            f"[yellow]🛑 Autostop:[/yellow] Stopping {vm_id} "
            f"(reason: {reason.value})"
        )
        
        # Callback
        if self._on_idle_callback:
            try:
                self._on_idle_callback(vm_id, reason)
            except Exception as e:
                logger.warning(f"Idle callback error: {e}")
        
        try:
            provider = get_provider(provider_name)
            
            if config.stop_on_idle and hasattr(provider, 'stop'):
                provider.stop(vm_id)
                self._state.update_status(vm_id, 'stopped')
                console.print(f"[green]✓[/green] {vm_id} stopped by autostop")
            elif hasattr(provider, 'terminate'):
                provider.terminate(vm_id)
                self._state.update_status(vm_id, 'terminated')
                console.print(f"[green]✓[/green] {vm_id} terminated by autostop")
            else:
                console.print(f"[yellow]Provider '{provider_name}' does not support stop/terminate[/yellow]")
            
            # Callback
            if self._on_stop_callback:
                try:
                    self._on_stop_callback(vm_id)
                except Exception as e:
                    logger.warning(f"Stop callback error: {e}")
                    
        except Exception as e:
            console.print(f"[red]Autostop failed for {vm_id}: {e}[/red]")
        
        # Unregister after handling
        self.unregister(vm_id)
    
    def get_vm_status(self, vm_id: str) -> Optional[Dict[str, Any]]:
        """
        Get autostop status for a VM.
        
        Returns:
            Dict with idle status, metrics history, etc.
        """
        tracking = self._tracked_vms.get(vm_id)
        if not tracking:
            return None
        
        config = tracking['config']
        idle_duration = datetime.now() - tracking['last_active']
        
        return {
            'vm_id': vm_id,
            'timeout_minutes': config.idle_timeout_minutes,
            'idle_minutes': idle_duration.total_seconds() / 60,
            'consecutive_idle_checks': tracking['consecutive_idle'],
            'required_idle_checks': config.require_consecutive_idle,
            'last_active': tracking['last_active'].isoformat(),
            'warned': tracking['warned'],
            'recent_metrics': [
                {
                    'cpu': m.cpu_percent,
                    'gpu': m.gpu_percent,
                    'memory': m.memory_percent,
                    'timestamp': m.timestamp.isoformat(),
                }
                for m in tracking['metrics_history'][-5:]
            ],
        }
    
    def check_once(self, vm_id: Optional[str] = None):
        """
        Run a single check cycle (useful for testing).
        
        Args:
            vm_id: Specific VM to check, or all if None
        """
        if vm_id:
            self._check_vm(vm_id)
        else:
            self._check_all_vms()


# Global agent instance
_agent: Optional[AutostopAgent] = None


def get_autostop_agent() -> AutostopAgent:
    """Get or create the global autostop agent."""
    global _agent
    if _agent is None:
        _agent = AutostopAgent()
    return _agent


def start_autostop_daemon():
    """Start the global autostop daemon."""
    agent = get_autostop_agent()
    agent.start()


def stop_autostop_daemon():
    """Stop the global autostop daemon."""
    global _agent
    if _agent:
        _agent.stop()
