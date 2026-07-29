"""Tests for the configuration module."""

import pytest
import yaml
from pathlib import Path
from minisky.config import MiniSkyConfig, _deep_merge


@pytest.fixture
def tmp_config(tmp_path):
    """Create a config instance with a temporary file."""
    config_file = tmp_path / "config.yaml"
    return MiniSkyConfig(config_path=str(config_file))


class TestMiniSkyConfig:
    """Tests for MiniSkyConfig class."""

    def test_defaults_loaded(self, tmp_config):
        """Config should return default values when no file exists."""
        assert tmp_config.get('default_provider') == 'mock'
        assert tmp_config.get('ssh.default_user') == 'root'
        assert tmp_config.get('ssh.retries') == 3

    def test_set_and_get(self, tmp_config):
        """Setting a value should persist and be retrievable."""
        tmp_config.set('default_provider', 'runpod')
        assert tmp_config.get('default_provider') == 'runpod'

    def test_set_nested(self, tmp_config):
        """Setting a nested key via dot notation should work."""
        tmp_config.set('providers.runpod.api_key', 'rp_test123')
        assert tmp_config.get('providers.runpod.api_key') == 'rp_test123'

    def test_get_missing_key(self, tmp_config):
        """Getting a nonexistent key should return None or default."""
        assert tmp_config.get('nonexistent') is None
        assert tmp_config.get('nonexistent', 'fallback') == 'fallback'

    def test_unset(self, tmp_config):
        """Unsetting a key should remove it."""
        tmp_config.set('custom_key', 'value')
        assert tmp_config.get('custom_key') == 'value'
        result = tmp_config.unset('custom_key')
        assert result is True
        assert tmp_config.get('custom_key') is None

    def test_unset_missing(self, tmp_config):
        """Unsetting a nonexistent key should return False."""
        result = tmp_config.unset('does_not_exist')
        assert result is False

    def test_show(self, tmp_config):
        """Show should return the full config dict."""
        data = tmp_config.show()
        assert isinstance(data, dict)
        assert 'default_provider' in data
        assert 'ssh' in data
        assert 'providers' in data

    def test_persistence(self, tmp_path):
        """Config should persist to disk and survive reload."""
        config_file = tmp_path / "config.yaml"

        # Write
        cfg1 = MiniSkyConfig(config_path=str(config_file))
        cfg1.set('providers.runpod.api_key', 'rp_persist_test')

        # Reload from same file
        cfg2 = MiniSkyConfig(config_path=str(config_file))
        assert cfg2.get('providers.runpod.api_key') == 'rp_persist_test'

    def test_reset(self, tmp_config):
        """Reset should restore defaults."""
        tmp_config.set('default_provider', 'runpod')
        tmp_config.reset()
        assert tmp_config.get('default_provider') == 'mock'

    def test_log_dir(self, tmp_config):
        """Log dir should be created and returned."""
        log_dir = tmp_config.log_dir
        assert log_dir.exists()
        assert log_dir.is_dir()


class TestDeepMerge:
    """Tests for the _deep_merge utility."""

    def test_simple_merge(self):
        base = {'a': 1, 'b': 2}
        override = {'b': 3, 'c': 4}
        result = _deep_merge(base, override)
        assert result == {'a': 1, 'b': 3, 'c': 4}

    def test_nested_merge(self):
        base = {'ssh': {'user': 'root', 'port': 22}}
        override = {'ssh': {'user': 'ubuntu'}}
        result = _deep_merge(base, override)
        assert result == {'ssh': {'user': 'ubuntu', 'port': 22}}

    def test_override_does_not_mutate_base(self):
        base = {'a': {'b': 1}}
        override = {'a': {'b': 2}}
        _deep_merge(base, override)
        assert base == {'a': {'b': 1}}
