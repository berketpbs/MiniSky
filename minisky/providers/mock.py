"""
Mock cloud provider for testing and development.

This provider simulates cloud operations without making real API calls
or launching actual VMs. Perfect for development and testing.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Any
from .base import BaseProvider, VMInfo, ProviderError

_DEFAULT_STATE_FILE = Path.home() / '.minisky' / 'mock_provider_state.json'


class MockProvider(BaseProvider):
    """
    Mock provider that simulates cloud operations.

    Features:
    - Generates fake VM IDs and IPs
    - Tracks VMs in a local JSON file (~/.minisky/mock_provider_state.json
      by default), not just in memory - every real provider's state lives
      on the actual cloud API, outside the calling process, so a fresh
      `minisky status`/`terminate`/etc. invocation can still see what a
      previous `minisky launch` created. An in-memory-only mock would
      "forget" every VM the instant that process exited, breaking any
      multi-command workflow (including this provider's only real use:
      trying MiniSky end-to-end without real cloud credentials).
    - Simulates API delays
    - No actual infrastructure required
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._simulate_delay = config.get('simulate_delay', True) if config else True
        state_file = config.get('state_file') if config else None
        self._state_file = Path(state_file) if state_file else _DEFAULT_STATE_FILE
        self._instances: Dict[str, VMInfo] = self._load()

    def _load(self) -> Dict[str, VMInfo]:
        """Load persisted instance state, if any."""
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self):
        """Persist instance state. Best-effort, like this codebase's
        other local caches (e.g. catalog.py's GPU price cache) - a write
        failure here shouldn't break the operation that triggered it."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._instances, indent=2))
        except OSError:
            pass
    
    def _generate_vm_id(self) -> str:
        """Generate a fake VM ID."""
        return f"mock-{uuid.uuid4().hex[:8]}"
    
    def _generate_ip(self) -> str:
        """Generate a fake IP address (localhost for testing)."""
        # Use localhost so we can actually SSH to it for testing
        return "127.0.0.1"
    
    def _simulate_api_delay(self, seconds: float = 0.5):
        """Simulate API response time."""
        if self._simulate_delay:
            time.sleep(seconds)
    
    def launch(self, task: Any) -> VMInfo:
        """
        Simulate launching a VM instance.
        
        Args:
            task: Task definition
            
        Returns:
            VMInfo with fake instance details
        """
        self._simulate_api_delay(1.0)  # Simulate launch time
        
        vm_id = self._generate_vm_id()
        vm_info: VMInfo = {
            'vm_id': vm_id,
            'ip_address': self._generate_ip(),
            'ssh_port': 22,
            'ssh_user': 'root',
            'status': 'running',
            'provider': 'mock',
            'task_name': task.name,
            'resources': task.resources.model_dump(),
            'created_at': time.time()
        }
        
        self._instances[vm_id] = vm_info
        self._save()
        return vm_info
    
    def status(self, vm_id: str) -> VMInfo:
        """
        Get status of a mock VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            VMInfo with current status
            
        Raises:
            ProviderError: If VM not found
        """
        self._simulate_api_delay(0.2)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        return self._instances[vm_id]
    
    def terminate(self, vm_id: str) -> bool:
        """
        Simulate terminating a VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if successful
            
        Raises:
            ProviderError: If VM not found
        """
        self._simulate_api_delay(0.5)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        self._instances[vm_id]['status'] = 'terminated'
        del self._instances[vm_id]
        self._save()
        return True
    
    def stop(self, vm_id: str) -> bool:
        """
        Simulate stopping a VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if successful
            
        Raises:
            ProviderError: If VM not found or not running
        """
        self._simulate_api_delay(0.4)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        if self._instances[vm_id]['status'] != 'running':
            raise ProviderError(f"VM {vm_id} is not running (status: {self._instances[vm_id]['status']})")
        
        self._instances[vm_id]['status'] = 'stopped'
        self._save()
        return True
    
    def start(self, vm_id: str) -> bool:
        """
        Simulate starting a stopped VM.
        
        Args:
            vm_id: VM identifier
            
        Returns:
            True if successful
            
        Raises:
            ProviderError: If VM not found or not stopped
        """
        self._simulate_api_delay(0.6)
        
        if vm_id not in self._instances:
            raise ProviderError(f"VM not found: {vm_id}")
        
        if self._instances[vm_id]['status'] != 'stopped':
            raise ProviderError(f"VM {vm_id} is not stopped (status: {self._instances[vm_id]['status']})")
        
        self._instances[vm_id]['status'] = 'running'
        self._save()
        return True
    
    def list_instances(self) -> List[VMInfo]:
        """
        List all mock instances.
        
        Returns:
            List of VMInfo dictionaries
        """
        self._simulate_api_delay(0.3)
        return list(self._instances.values())
