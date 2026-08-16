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

    def is_aws_configured(self) -> bool:
        """
        Check if AWS credentials are available.

        Unlike RunPod/Lambda, AWS doesn't use a single API key - this
        checks MiniSky's own config first, then falls back to boto3's
        standard credential chain (env vars, ~/.aws/credentials, IAM role),
        matching how the AWS CLI itself resolves credentials.

        Returns:
            True if AWS credentials are available from any source
        """
        access_key = self._config.get('providers.aws.access_key_id')
        secret_key = self._config.get('providers.aws.secret_access_key')
        if access_key and secret_key:
            return True

        try:
            import boto3
            return boto3.Session().get_credentials() is not None
        except Exception:
            return False

    def is_gcp_configured(self) -> bool:
        """
        Check if GCP credentials and a project are available.

        Like AWS, GCP doesn't use a single API key - this requires
        providers.gcp.project to be set (GCP has no sensible default
        project) and credentials resolving via either an explicit
        providers.gcp.credentials_path or google-auth's standard chain
        (GOOGLE_APPLICATION_CREDENTIALS, `gcloud auth application-default
        login`, or the GCE metadata server).

        Returns:
            True if a project is configured and credentials are available
        """
        if not self._config.get('providers.gcp.project'):
            return False

        if self._config.get('providers.gcp.credentials_path'):
            return True

        try:
            import google.auth
            google.auth.default()
            return True
        except Exception:
            return False

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
        if self.is_aws_configured():
            configured.append('aws')
        if self.is_gcp_configured():
            configured.append('gcp')
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
