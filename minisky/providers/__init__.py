"""
Cloud provider implementations.

This module contains the base provider interface and
implementations for various cloud providers.
"""

from .base import BaseProvider, ProviderError, VMInfo
from .mock import MockProvider

# Provider registry
_PROVIDERS = {
    'mock': MockProvider,
}


def get_provider(provider_name: str, config: dict = None) -> BaseProvider:
    """
    Get a provider instance by name.
    
    Args:
        provider_name: Name of the provider (e.g., 'mock', 'runpod')
        config: Optional provider configuration
        
    Returns:
        Provider instance
        
    Raises:
        ValueError: If provider not found
    """
    provider_name = provider_name.lower()
    
    if provider_name not in _PROVIDERS:
        available = ', '.join(_PROVIDERS.keys())
        raise ValueError(
            f"Provider '{provider_name}' not found. "
            f"Available providers: {available}"
        )
    
    provider_class = _PROVIDERS[provider_name]
    return provider_class(config)


def register_provider(name: str, provider_class: type):
    """
    Register a new provider.
    
    Args:
        name: Provider name
        provider_class: Provider class (must inherit from BaseProvider)
    """
    if not issubclass(provider_class, BaseProvider):
        raise TypeError("Provider must inherit from BaseProvider")
    
    _PROVIDERS[name.lower()] = provider_class


__all__ = [
    "BaseProvider",
    "ProviderError",
    "VMInfo",
    "MockProvider",
    "get_provider",
    "register_provider",
]
