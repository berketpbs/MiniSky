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

    def test_result_does_not_alias_base_nested_dicts(self):
        """
        With an empty override, a naive `base.copy()` (shallow) returns
        nested dicts that are the *same objects* as in base - so mutating
        the result in place (e.g. via MiniSkyConfig.set) would silently
        corrupt base too. This must not happen even when override is {}.
        """
        base = {'providers': {'runpod': {'api_key': None}}}
        result = _deep_merge(base, {})
        result['providers']['runpod']['api_key'] = 'leaked-key'
        assert base['providers']['runpod']['api_key'] is None

    def test_module_defaults_not_corrupted_by_instance_set(self, tmp_path):
        """
        Regression test for a real bug: MiniSkyConfig instances pointed at
        different (or nonexistent) config files must be fully independent.
        Previously, calling .set() on a fresh config (empty override, so
        it merges straight from the module-level _DEFAULTS) mutated
        _DEFAULTS in place, leaking credentials into every other
        MiniSkyConfig instance created afterward in the same process.
        """
        cfg_a = MiniSkyConfig(config_path=str(tmp_path / "a.yaml"))
        cfg_a.set('providers.runpod.api_key', 'leaked-key')

        cfg_b = MiniSkyConfig(config_path=str(tmp_path / "b.yaml"))
        assert cfg_b.get('providers.runpod.api_key') is None
