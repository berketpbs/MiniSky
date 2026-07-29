"""
Configuration management for MiniSky.

Handles reading/writing user configuration from ~/.minisky/config.yaml.
Stores provider API keys, default settings, and user preferences.
"""

import yaml
from pathlib import Path
from typing import Any, Dict, Optional


# Default configuration values
_DEFAULTS: Dict[str, Any] = {
    'default_provider': 'mock',
    'default_region': None,
    'autostop_minutes': 30,
    'ssh': {
        'default_user': 'root',
        'default_key_path': None,
        'connect_timeout': 30,
        'retries': 3,
    },
    'providers': {
        'runpod': {
            'api_key': None,
        },
        'lambda': {
            'api_key': None,
        },
    },
    'logging': {
        'level': 'INFO',
        'log_dir': None,  # Defaults to ~/.minisky/logs/
    },
}


class MiniSkyConfig:
    """
    Manages MiniSky configuration stored in ~/.minisky/config.yaml.

    Configuration is lazily loaded on first access and cached in memory.
    Changes are written back to disk immediately.

    Usage:
        config = MiniSkyConfig()
        config.get('default_provider')          # -> 'mock'
        config.get('ssh.default_user')          # -> 'root'
        config.set('providers.runpod.api_key', 'rp_xxx')
        config.show()                           # -> full config dict
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize configuration manager.

        Args:
            config_path: Custom path to config file.
                         Defaults to ~/.minisky/config.yaml
        """
        if config_path is None:
            self._config_dir = Path.home() / '.minisky'
            self._config_dir.mkdir(exist_ok=True)
            self._config_path = self._config_dir / 'config.yaml'
        else:
            self._config_path = Path(config_path)
            self._config_dir = self._config_path.parent

        self._data: Optional[Dict[str, Any]] = None

    @property
    def config_dir(self) -> Path:
        """Return the MiniSky configuration directory."""
        return self._config_dir

    @property
    def log_dir(self) -> Path:
        """Return the log directory, creating it if needed."""
        custom = self.get('logging.log_dir')
        if custom:
            p = Path(custom)
        else:
            p = self._config_dir / 'logs'
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _load(self) -> Dict[str, Any]:
        """Load config from disk, merging with defaults."""
        if self._config_path.exists():
            with open(self._config_path, 'r') as f:
                user_data = yaml.safe_load(f) or {}
        else:
            user_data = {}

        return _deep_merge(_DEFAULTS, user_data)

    def _ensure_loaded(self):
        """Ensure configuration is loaded into memory."""
        if self._data is None:
            self._data = self._load()

    def _save(self):
        """Write current config state to disk."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, 'w') as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value using dot-notation.

        Args:
            key: Dot-separated key path (e.g. 'ssh.default_user')
            default: Fallback value if key is not found

        Returns:
            The configuration value, or default if not found.
        """
        self._ensure_loaded()
        parts = key.split('.')
        current = self._data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    def set(self, key: str, value: Any):
        """
        Set a configuration value using dot-notation and persist to disk.

        Args:
            key: Dot-separated key path (e.g. 'providers.runpod.api_key')
            value: Value to set
        """
        self._ensure_loaded()
        parts = key.split('.')
        current = self._data
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
        self._save()

    def unset(self, key: str) -> bool:
        """
        Remove a configuration key and persist to disk.

        Args:
            key: Dot-separated key path

        Returns:
            True if key was found and removed, False otherwise.
        """
        self._ensure_loaded()
        parts = key.split('.')
        current = self._data
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False
        if parts[-1] in current:
            del current[parts[-1]]
            self._save()
            return True
        return False

    def show(self) -> Dict[str, Any]:
        """
        Return the full configuration dictionary.

        Returns:
            Complete configuration as a dictionary.
        """
        self._ensure_loaded()
        return self._data.copy()

    def reset(self):
        """Reset configuration to defaults and persist."""
        self._data = _DEFAULTS.copy()
        self._save()


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """
    Recursively merge override dict into base dict.
    Values in override take precedence.

    Args:
        base: Base dictionary (defaults)
        override: Override dictionary (user config)

    Returns:
        Merged dictionary
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
