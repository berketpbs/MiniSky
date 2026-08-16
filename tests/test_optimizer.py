"""Tests for the cost optimizer (minisky/optimizer.py)."""

import pytest
from unittest.mock import patch, MagicMock
from io import StringIO

from minisky.optimizer import CostOptimizer, OptimizerResult
from minisky.task import Task, ResourceRequirements


def _optimizer(tmp_path):
    from minisky.config import MiniSkyConfig
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))
    return CostOptimizer(config=config)


class TestOptimizerResult:
    def test_effective_price_with_spot(self):
        r = OptimizerResult(provider="p", gpu_name="A100", price_per_hour=5.0, spot_price=2.0)
        assert r.effective_price == 2.0

    def test_effective_price_without_spot(self):
        r = OptimizerResult(provider="p", gpu_name="A100", price_per_hour=5.0)
        assert r.effective_price == 5.0

    def test_effective_price_zero_spot(self):
        r = OptimizerResult(provider="p", gpu_name="A100", price_per_hour=5.0, spot_price=0.0)
        # spot_price=0 is falsy, so falls back to on-demand
        assert r.effective_price == 5.0

    def test_repr(self):
        r = OptimizerResult(provider="runpod", gpu_name="A100", price_per_hour=1.50, spot_price=0.80)
        repr_str = repr(r)
        assert "runpod" in repr_str
        assert "A100" in repr_str
        assert "1.50" in repr_str

    def test_default_available(self):
        r = OptimizerResult(provider="p", gpu_name="g", price_per_hour=1.0)
        assert r.available is True


class TestFindAllPriceSorting:
    """A legitimately free ($0/hr) candidate must sort first, not last."""

    def test_zero_price_candidate_ranks_before_paid_ones(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: name == "runpod"
        optimizer._query_runpod = lambda gpu_name, gpu_count: [
            OptimizerResult(provider="runpod", gpu_name="A100", price_per_hour=5.0, available=True),
            OptimizerResult(provider="runpod-free-tier", gpu_name="A100", price_per_hour=0.0, available=True),
        ]

        task = Task(name="t", run=["echo hi"], resources=ResourceRequirements(gpu="A100"))
        results = optimizer.find_all(task)

        assert results[0].price_per_hour == 0.0
        assert results[0].provider == "runpod-free-tier"

    def test_zero_spot_price_used_when_preferring_spot(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: name == "runpod"
        optimizer._query_runpod = lambda gpu_name, gpu_count: [
            OptimizerResult(provider="runpod", gpu_name="A100", price_per_hour=5.0, spot_price=2.0, available=True),
            OptimizerResult(provider="runpod-promo", gpu_name="A100", price_per_hour=5.0, spot_price=0.0, available=True),
        ]

        task = Task(
            name="t",
            run=["echo hi"],
            resources=ResourceRequirements(gpu="A100", use_spot=True),
        )
        results = optimizer.find_all(task)

        assert results[0].provider == "runpod-promo"

    def test_mock_fallback_still_ranks_first_when_alone(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: False

        task = Task(name="t", run=["echo hi"], resources=ResourceRequirements(gpu="A100"))
        results = optimizer.find_all(task)

        assert len(results) == 1
        assert results[0].provider == "mock"
        assert results[0].price_per_hour == 0.0


class TestFindBest:
    def test_find_best_returns_cheapest(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: name == "runpod"
        optimizer._query_runpod = lambda gpu_name, gpu_count: [
            OptimizerResult(provider="runpod", gpu_name="A100", price_per_hour=3.0, available=True),
            OptimizerResult(provider="runpod", gpu_name="A100", price_per_hour=1.0, available=True),
        ]

        task = Task(name="t", run=["echo"], resources=ResourceRequirements(gpu="A100"))
        best = optimizer.find_best(task)
        assert best is not None
        assert best.price_per_hour == 0.0  # Mock is always added and is cheapest

    def test_find_best_no_gpu_no_candidates(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: False

        task = Task(name="t", run=["echo"], resources=ResourceRequirements())
        best = optimizer.find_best(task)
        # No GPU specified → no mock fallback added
        assert best is None

    def test_find_best_prefer_spot(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: name == "runpod"
        optimizer._query_runpod = lambda gpu_name, gpu_count: [
            OptimizerResult(provider="runpod-a", gpu_name="A100", price_per_hour=5.0, spot_price=1.5, available=True),
            OptimizerResult(provider="runpod-b", gpu_name="A100", price_per_hour=2.0, spot_price=None, available=True),
        ]

        task = Task(name="t", run=["echo"], resources=ResourceRequirements(gpu="A100"))
        best = optimizer.find_best(task, prefer_spot=True)
        # Mock ($0) should still be first
        assert best.provider == "mock"


class TestUnavailableSorting:
    def test_unavailable_sorts_after_available(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: name == "runpod"
        optimizer._query_runpod = lambda gpu_name, gpu_count: [
            OptimizerResult(provider="runpod-cheap", gpu_name="A100", price_per_hour=0.5, available=False),
            OptimizerResult(provider="runpod-avail", gpu_name="A100", price_per_hour=5.0, available=True),
        ]

        task = Task(name="t", run=["echo"], resources=ResourceRequirements(gpu="A100"))
        results = optimizer.find_all(task)
        # Available ones should come before unavailable
        available_indices = [i for i, r in enumerate(results) if r.available]
        unavailable_indices = [i for i, r in enumerate(results) if not r.available]
        if available_indices and unavailable_indices:
            assert max(available_indices) < min(unavailable_indices)


class TestDisplayOptions:
    def test_display_with_results_does_not_crash(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: False

        task = Task(name="t", run=["echo"], resources=ResourceRequirements(gpu="A100"))
        # Should not raise
        optimizer.display_options(task)

    def test_display_no_results(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        optimizer._creds.is_configured = lambda name: False

        task = Task(name="t", run=["echo"], resources=ResourceRequirements())
        # No GPU → no mock → empty results
        optimizer.display_options(task)


class TestQueryProviders:
    def test_query_runpod_error_returns_empty(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        # _query_runpod catches all exceptions and returns []
        with patch("minisky.optimizer.RunPodProvider", side_effect=Exception("boom")):
            result = optimizer._query_runpod("A100", 1)
            assert result == []

    def test_query_lambda_error_returns_empty(self, tmp_path):
        optimizer = _optimizer(tmp_path)
        with patch("minisky.optimizer.LambdaProvider", side_effect=Exception("boom")):
            result = optimizer._query_lambda("A100", 1)
            assert result == []
