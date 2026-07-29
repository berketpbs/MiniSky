"""
Autostop daemon for MiniSky.

Monitors running VMs and automatically stops them after
a configured idle period to prevent unnecessary cloud costs.
"""

import time
import threading
from typing import Optional, Dict
from datetime import datetime, timedelta
from rich.console import Console

from .config import MiniSkyConfig
from .state import StateManager
from .providers import get_provider

console = Console()


class AutostopManager:
    """
    Monitors VMs and stops them after idle timeout.

    The autostop daemon runs as a background thread, periodically
    checking each VM's last activity timestamp. If a VM has been
    idle longer than its configured autostop timeout, it is stopped.

    Idle tracking is based on the last state update timestamp
    stored in the SQLite database.
    """

    def __init__(
        self,
        config: Optional[MiniSkyConfig] = None,
        state: Optional[StateManager] = None,
    ):
        self._config = config or MiniSkyConfig()
        self._state = state or StateManager()
        self._default_timeout = self._config.get('autostop_minutes', 30)
        self._check_interval = 60  # Check every 60 seconds
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._vm_timeouts: Dict[str, int] = {}  # vm_id -> timeout minutes

    def register(self, vm_id: str, timeout_minutes: Optional[int] = None):
        """
        Register a VM for autostop monitoring.

        Args:
            vm_id: VM identifier
            timeout_minutes: Idle timeout in minutes (uses default if None)
        """
        timeout = timeout_minutes or self._default_timeout
        self._vm_timeouts[vm_id] = timeout
        console.print(
            f"[cyan]Autostop:[/cyan] {vm_id} will stop after {timeout} idle minutes"
        )

    def unregister(self, vm_id: str):
        """
        Remove a VM from autostop monitoring.

        Args:
            vm_id: VM identifier
        """
        self._vm_timeouts.pop(vm_id, None)

    def start_daemon(self):
        """Start the autostop monitoring daemon as a background thread."""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="autostop-daemon",
        )
        self._thread.start()

    def stop_daemon(self):
        """Stop the autostop monitoring daemon."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _monitor_loop(self):
        """Main monitoring loop running in background thread."""
        while self._running:
            try:
                self._check_idle_vms()
            except Exception as e:
                # Log but don't crash the daemon
                console.print(f"[yellow]Autostop error: {e}[/yellow]")

            time.sleep(self._check_interval)

    def _check_idle_vms(self):
        """Check all registered VMs for idle timeout."""
        for vm_id, timeout_minutes in list(self._vm_timeouts.items()):
            vm_info = self._state.get_vm(vm_id)

            if not vm_info:
                # VM no longer exists, unregister
                self.unregister(vm_id)
                continue

            if vm_info['status'] != 'running':
                continue

            # Check idle time based on last update
            updated_at = vm_info.get('updated_at', '')
            if not updated_at:
                continue

            try:
                last_update = datetime.fromisoformat(str(updated_at))
                idle_duration = datetime.now() - last_update
                timeout_delta = timedelta(minutes=timeout_minutes)

                if idle_duration > timeout_delta:
                    self._stop_vm(vm_id, vm_info, idle_duration)
            except (ValueError, TypeError):
                # Can't parse timestamp, skip
                continue

    def _stop_vm(self, vm_id: str, vm_info: dict, idle_duration: timedelta):
        """
        Stop an idle VM.

        Args:
            vm_id: VM identifier
            vm_info: VM information from state
            idle_duration: How long the VM has been idle
        """
        idle_minutes = int(idle_duration.total_seconds() / 60)
        provider_name = vm_info.get('provider', 'unknown')

        console.print(
            f"[yellow]Autostop:[/yellow] Stopping {vm_id} "
            f"(idle {idle_minutes} minutes)"
        )

        try:
            provider = get_provider(provider_name)
            if hasattr(provider, 'stop'):
                provider.stop(vm_id)
                self._state.update_status(vm_id, 'stopped')
                console.print(f"[green]>[/green] {vm_id} stopped by autostop")
            else:
                console.print(
                    f"[yellow]Provider '{provider_name}' does not support stop. "
                    f"Consider terminating manually.[/yellow]"
                )
        except Exception as e:
            console.print(f"[red]Autostop failed for {vm_id}: {e}[/red]")

        # Unregister after stopping
        self.unregister(vm_id)

    def check_once(self):
        """Run a single check cycle (useful for testing)."""
        self._check_idle_vms()
