"""Tests for the price fetcher (minisky/price_fetcher.py)."""

from unittest.mock import patch

from minisky.price_fetcher import RealTimePriceFetcher, GPUPrice


def _price(provider, price_per_hour, spot_price=None, gpu_name="A100", availability="available"):
    return GPUPrice(
        provider=provider,
        gpu_name=gpu_name,
        gpu_id=gpu_name,
        gpu_count=1,
        vram_gb=80,
        price_per_hour=price_per_hour,
        spot_price=spot_price,
        availability=availability,
    )


class TestFindCheapest:
    def test_zero_price_ranks_first_not_last(self, tmp_path):
        from minisky.config import MiniSkyConfig
        fetcher = RealTimePriceFetcher(config=MiniSkyConfig(config_path=str(tmp_path / "config.yaml")))
        prices = [_price("expensive", 5.0), _price("free-tier", 0.0)]

        with patch.object(fetcher, "fetch_all", return_value=prices):
            cheapest = fetcher.find_cheapest(gpu_name="A100")

        assert cheapest.provider == "free-tier"
        assert cheapest.price_per_hour == 0.0

    def test_zero_spot_price_used_when_preferring_spot(self, tmp_path):
        from minisky.config import MiniSkyConfig
        fetcher = RealTimePriceFetcher(config=MiniSkyConfig(config_path=str(tmp_path / "config.yaml")))
        prices = [
            _price("normal", 5.0, spot_price=2.0),
            _price("promo", 5.0, spot_price=0.0),
        ]

        with patch.object(fetcher, "fetch_all", return_value=prices):
            cheapest = fetcher.find_cheapest(gpu_name="A100", prefer_spot=True)

        assert cheapest.provider == "promo"


class TestComparePrices:
    def test_sorts_zero_price_first(self, tmp_path):
        from minisky.config import MiniSkyConfig
        fetcher = RealTimePriceFetcher(config=MiniSkyConfig(config_path=str(tmp_path / "config.yaml")))
        prices = [_price("expensive", 5.0), _price("free-tier", 0.0), _price("mid", 2.0)]

        with patch.object(fetcher, "fetch_all", return_value=prices):
            results = fetcher.compare_prices(gpu_name="A100")

        assert [r.provider for r in results] == ["free-tier", "mid", "expensive"]
