"""
Abstract base class for cloud providers.

All provider implementations must inherit from BaseProvider
and implement all abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


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
    def stop(self, vm_id: str) -> bool:
        """
        Stop a VM instance (but keep disk).
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            True if stop successful
            
        Raises:
            ProviderError: If stop fails
        """
        pass
    
    @abstractmethod
    def start(self, vm_id: str) -> bool:
        """
        Start a stopped VM instance.
        
        Args:
            vm_id: Unique VM identifier
            
        Returns:
            True if start successful
            
        Raises:
            ProviderError: If start fails
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

    def abort_launch(
        self,
        resource_desc: str,
        cleanup: Callable[[], Any],
        error: Exception,
    ) -> ProviderError:
        """
        Tear down a resource that was created but never became usable.

        Every provider's launch() is two steps: create the instance, then wait
        for it to come up. If the second step fails - the instance never gets
        an IP, the API times out, the user hits Ctrl-C - the first step has
        already happened and the cloud is billing for it. Without this, that
        instance is orphaned: MiniSky raises, never records it in the state DB,
        and so no `minisky status`, `terminate` or autostop will ever see it
        again. It just runs until someone notices the invoice.

        Cleanup is best-effort. If it also fails, the resource id goes into the
        raised error rather than being swallowed - a user who has to kill an
        instance by hand at least needs to know its id.

        Args:
            resource_desc: Human-readable id, e.g. "pod abc123"
            cleanup: Callable that terminates the resource
            error: The failure that made the resource useless

        Returns:
            The ProviderError to raise (never raises on its own, so callers
            keep `raise` at the call site and static analysis stays happy).
        """
        # KeyboardInterrupt and friends stringify to "", which would leave the
        # message reading "never became usable: ." - fall back to the type name.
        reason = str(error) or type(error).__name__

        try:
            cleanup()
            return ProviderError(
                f"{resource_desc} never became usable: {reason}. "
                f"It has been terminated, so it is not still billing."
            )
        except Exception as cleanup_error:
            return ProviderError(
                f"{resource_desc} never became usable: {reason}. "
                f"Cleaning it up ALSO failed ({cleanup_error}), so it may still "
                f"be running and billing - terminate it manually."
            )
