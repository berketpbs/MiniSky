"""
Credential management for cloud providers.

Handles API key storage, validation, and retrieval
from config file or environment variables.
"""

import os
from typing import Optional
from .config import MiniSkyConfig


class CredentialManager:
    """
    Manages API credentials for cloud providers.

    Credentials are resolved in priority order:
    1. Environment variable (e.g. RUNPOD_API_KEY)
    2. Config file (~/.minisky/config.yaml)
    3. None (not configured)
    """

    # Mapping of provider name -> environment variable name
    _ENV_VARS = {
        'runpod': 'RUNPOD_API_KEY',
        'lambda': 'LAMBDA_API_KEY',
    }

    def __init__(self, config: Optional[MiniSkyConfig] = None):
        """
        Initialize credential manager.

        Args:
            config: MiniSky configuration instance
        """
        self._config = config or MiniSkyConfig()

    def get_api_key(self, provider: str) -> Optional[str]:
        """
        Get API key for a provider.

        Resolution order:
        1. Environment variable
        2. Config file
        3. None

        Args:
            provider: Provider name (e.g. 'runpod', 'lambda')

        Returns:
            API key string or None if not configured
        """
        provider = provider.lower()

        # 1. Check environment variable
        env_var = self._ENV_VARS.get(provider)
        if env_var:
            env_value = os.environ.get(env_var)
            if env_value:
                return env_value

        # 2. Check config file
        config_key = f"providers.{provider}.api_key"
        config_value = self._config.get(config_key)
        if config_value:
            return config_value

        return None

    def set_api_key(self, provider: str, api_key: str):
        """
        Store an API key in the config file.

        Args:
            provider: Provider name
            api_key: API key to store
        """
        config_key = f"providers.{provider.lower()}.api_key"
        self._config.set(config_key, api_key)

    def is_configured(self, provider: str) -> bool:
        """
        Check if a provider has credentials configured.

        Args:
            provider: Provider name

        Returns:
            True if API key is available
        """
        return self.get_api_key(provider) is not None

    def get_configured_providers(self) -> list:
        """
        List all providers that have credentials configured.

        Returns:
            List of provider names with valid credentials
        """
        configured = ['mock']  # Mock is always available
        for provider in self._ENV_VARS:
            if self.is_configured(provider):
                configured.append(provider)
        return configured

    def require_api_key(self, provider: str) -> str:
        """
        Get API key or raise an error if not configured.

        Args:
            provider: Provider name

        Returns:
            API key string

        Raises:
            ValueError: If no API key is found
        """
        key = self.get_api_key(provider)
        if not key:
            env_var = self._ENV_VARS.get(provider.lower(), f"{provider.upper()}_API_KEY")
            raise ValueError(
                f"No API key found for '{provider}'. "
                f"Set it via:\n"
                f"  1. Environment: export {env_var}=your_key\n"
                f"  2. Config: minisky config set providers.{provider}.api_key your_key"
            )
        return key
