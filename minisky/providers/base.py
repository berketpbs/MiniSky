"""
Abstract base class for cloud providers.

All provider implementations must inherit from BaseProvider
and implement all abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional


class ProviderError(Exception):
    """Base exception for provider errors."""
    pass


# Type alias for VM information dictionary
VMInfo = Dict[str, Any]


class BaseProvider(ABC):
    """
    Abstract base class for cloud providers.
    
    Each provider must implement methods to:
    - Launch new VM instances
    - Check status of instances
    - Terminate instances
    - List all instances
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize provider with configuration.
        
        Args:
            config: Provider-specific configuration (API keys, etc.)
        """
        self.config = config or {}
    
    @abstractmethod
    def launch(self, task: Any) -> VMInfo:
        """
        Launch a new VM instance based on task requirements.
        
        Args:
            task: Task definition with resource requirements
            
        Returns:
            VMInfo dictionary with instance details:
            {
                "vm_id": str,
                "ip_address": str,
                "ssh_port": int,
                "ssh_user": str,
                "status": str,
                "provider": str,
                "task_name": str
            }
            
        Raises:
            ProviderError: If launch fails
        """
        pass
    
    @abstractmethod
    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of a VM instance.
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            VMInfo dictionary with current status
            
        Raises:
            ProviderError: If VM not found or API error
        """
        pass
    
    @abstractmethod
    def terminate(self, vm_id: str) -> bool:
        """
        Terminate a VM instance.
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            True if termination successful
            
        Raises:
            ProviderError: If termination fails
        """
        pass
    
    @abstractmethod
    def list_instances(self) -> List[VMInfo]:
        """
        List all active instances managed by this provider.
        
        Returns:
            List of VMInfo dictionaries
            
        Raises:
            ProviderError: If API error
        """
        pass
    
    def validate_resources(self, task: Any) -> bool:
        """
        Validate that provider can fulfill resource requirements.
        
        Override this method to add provider-specific validation.
        
        Args:
            task: Task with resource requirements
            
        Returns:
            True if resources can be fulfilled
            
        Raises:
            ProviderError: If resources cannot be fulfilled
        """
        return True
