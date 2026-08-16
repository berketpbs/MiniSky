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

# Lazy-load real providers to avoid import errors when credentials aren't set
_LAZY_PROVIDERS = {
    'runpod': ('minisky.providers.runpod', 'RunPodProvider'),
    'lambda': ('minisky.providers.lambda_cloud', 'LambdaProvider'),
    'aws': ('minisky.providers.aws', 'AWSProvider'),
}


def get_provider(provider_name: str, config: dict = None) -> BaseProvider:
    """
    Get a provider instance by name.

    Args:
        provider_name: Name of the provider (e.g., 'mock', 'runpod', 'lambda')
        config: Optional provider configuration

    Returns:
        Provider instance

    Raises:
        ValueError: If provider not found
    """
    provider_name = provider_name.lower()

    # Check direct registry first
    if provider_name in _PROVIDERS:
        provider_class = _PROVIDERS[provider_name]
        return provider_class(config)

    # Try lazy-loaded providers
    if provider_name in _LAZY_PROVIDERS:
        module_path, class_name = _LAZY_PROVIDERS[provider_name]
        import importlib
        module = importlib.import_module(module_path)
        provider_class = getattr(module, class_name)
        # Cache for future lookups
        _PROVIDERS[provider_name] = provider_class
        return provider_class(config)

    available = ', '.join(list(_PROVIDERS.keys()) + list(_LAZY_PROVIDERS.keys()))
    raise ValueError(
        f"Provider '{provider_name}' not found. "
        f"Available providers: {available}"
    )


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


def list_available_providers() -> list:
    """
    List all registered and lazy-loaded provider names.

    Returns:
        List of provider name strings
    """
    return list(set(list(_PROVIDERS.keys()) + list(_LAZY_PROVIDERS.keys())))


__all__ = [
    "BaseProvider",
    "ProviderError",
    "VMInfo",
    "MockProvider",
    "get_provider",
    "register_provider",
    "list_available_providers",
]
