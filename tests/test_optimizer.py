"""Tests for the cost optimizer (minisky/optimizer.py)."""

from minisky.optimizer import CostOptimizer, OptimizerResult
from minisky.task import Task, ResourceRequirements


def _optimizer(tmp_path):
    from minisky.config import MiniSkyConfig
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))
    return CostOptimizer(config=config)


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
