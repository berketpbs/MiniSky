"""
Mock cloud provider for testing and development.

This provider simulates cloud operations without making real API calls
or launching actual VMs. Perfect for development and testing.
"""

import time
import uuid
from typing import Dict, List, Any
from .base import BaseProvider, VMInfo, ProviderError


class MockProvider(BaseProvider):
    """
    Mock provider that simulates cloud operations.
    
    Features:
    - Generates fake VM IDs and IPs
    - Tracks VMs in memory
    - Simulates API delays
    - No actual infrastructure required
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._instances: Dict[str, VMInfo] = {}
        self._simulate_delay = config.get('simulate_delay', True) if config else True
    
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
        return True
    
    def list_instances(self) -> List[VMInfo]:
        """
        List all mock instances.
        
        Returns:
            List of VMInfo dictionaries
        """
        self._simulate_api_delay(0.3)
        return list(self._instances.values())
