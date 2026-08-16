"""Tests for the GPU catalog (minisky/catalog.py)."""

import json

from minisky.catalog import GPUCatalog
from minisky.config import MiniSkyConfig


def _catalog(tmp_path):
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))
    return GPUCatalog(config=config)


class TestDisplayZeroPrice:
    def test_zero_price_entries_render_as_dollar_zero_not_dash(self, tmp_path, capsys):
        catalog = _catalog(tmp_path)
        # No providers configured -> fetch_all() falls back to just the
        # mock entries, which are legitimately $0.00/hr.
        catalog.display()

        out = capsys.readouterr().out
        assert "$0.00" in out
        assert "Mock A100" in out


class TestReadCacheResilience:
    def test_null_cached_at_treated_as_invalid_not_a_crash(self, tmp_path):
        catalog = _catalog(tmp_path)
        catalog._cache_path.parent.mkdir(parents=True, exist_ok=True)
        catalog._cache_path.write_text(json.dumps({"cached_at": None, "entries": []}))

        # Must not raise TypeError - should just treat the cache as
        # invalid/expired and fall through to a fresh fetch.
        result = catalog._read_cache()
        assert result is None

    def test_valid_cache_is_used(self, tmp_path):
        catalog = _catalog(tmp_path)
        entries = catalog.fetch_all()  # writes the cache
        assert catalog._cache_path.exists()

        cached = catalog._read_cache()
        assert cached == entries
