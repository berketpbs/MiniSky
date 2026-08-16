"""
Real-time price fetcher for MiniSky cost optimizer.

Fetches live GPU prices from cloud provider APIs:
- RunPod: Real-time pricing via GraphQL API
- Lambda Labs: Instance type pricing
- Vast.ai: Marketplace pricing (future)

Includes caching to avoid excessive API calls.
"""

import time
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

import httpx

from .config import MiniSkyConfig
from .credentials import CredentialManager

logger = logging.getLogger(__name__)


@dataclass
class GPUPrice:
    """Represents pricing for a GPU type."""
    provider: str
    gpu_name: str
    gpu_id: str
    gpu_count: int
    vram_gb: int
    price_per_hour: float
    spot_price: Optional[float] = None
    region: Optional[str] = None
    availability: str = "unknown"  # available, limited, unavailable
    last_updated: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        d = asdict(self)
        d['last_updated'] = self.last_updated.isoformat()
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GPUPrice':
        """Create from dictionary."""
        data['last_updated'] = datetime.fromisoformat(data['last_updated'])
        return cls(**data)


class PriceCache:
    """
    Caches GPU prices to avoid excessive API calls.
    
    Prices are cached for a configurable duration (default 5 minutes).
    """
    
    def __init__(self, cache_dir: Optional[Path] = None, ttl_seconds: int = 300):
        self.cache_dir = cache_dir or Path.home() / ".minisky" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._memory_cache: Dict[str, List[GPUPrice]] = {}
        self._cache_times: Dict[str, datetime] = {}
    
    def _cache_file(self, provider: str) -> Path:
        """Get cache file path for a provider."""
        return self.cache_dir / f"prices_{provider}.json"
    
    def get(self, provider: str) -> Optional[List[GPUPrice]]:
        """Get cached prices for a provider."""
        # Check memory cache first
        if provider in self._memory_cache:
            cache_time = self._cache_times.get(provider)
            if cache_time and (datetime.now() - cache_time).total_seconds() < self.ttl_seconds:
                return self._memory_cache[provider]
        
        # Check file cache
        cache_file = self._cache_file(provider)
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                cache_time = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
                
                if (datetime.now() - cache_time).total_seconds() < self.ttl_seconds:
                    prices = [GPUPrice.from_dict(p) for p in data.get('prices', [])]
                    self._memory_cache[provider] = prices
                    self._cache_times[provider] = cache_time
                    return prices
            except Exception as e:
                logger.warning(f"Failed to read price cache for {provider}: {e}")
        
        return None
    
    def set(self, provider: str, prices: List[GPUPrice]):
        """Cache prices for a provider."""
        self._memory_cache[provider] = prices
        self._cache_times[provider] = datetime.now()
        
        # Write to file
        cache_file = self._cache_file(provider)
        try:
            data = {
                'timestamp': datetime.now().isoformat(),
                'provider': provider,
                'prices': [p.to_dict() for p in prices]
            }
            cache_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to write price cache for {provider}: {e}")
    
    def invalidate(self, provider: Optional[str] = None):
        """Invalidate cache for a provider or all providers."""
        if provider:
            self._memory_cache.pop(provider, None)
            self._cache_times.pop(provider, None)
            cache_file = self._cache_file(provider)
            if cache_file.exists():
                cache_file.unlink()
        else:
            self._memory_cache.clear()
            self._cache_times.clear()
            for f in self.cache_dir.glob("prices_*.json"):
                f.unlink()


class RunPodPriceFetcher:
    """
    Fetches real-time GPU prices from RunPod API.
    
    Uses the GraphQL API to get current pricing and availability.
    """
    
    API_URL = "https://api.runpod.io/graphql"
    
    # GPU catalog query
    GPU_TYPES_QUERY = """
    query GpuTypes {
        gpuTypes {
            id
            displayName
            memoryInGb
            secureCloud
            communityCloud
            lowestPrice(input: {gpuCount: 1}) {
                minimumBidPrice
                uninterruptablePrice
            }
        }
    }
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def fetch_prices(self) -> List[GPUPrice]:
        """Fetch current GPU prices from RunPod."""
        prices = []
        
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    self.API_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}"
                    },
                    json={"query": self.GPU_TYPES_QUERY}
                )
                response.raise_for_status()
                data = response.json()
                
                gpu_types = data.get('data', {}).get('gpuTypes', [])
                
                for gpu in gpu_types:
                    lowest = gpu.get('lowestPrice', {}) or {}
                    spot_price = lowest.get('minimumBidPrice')
                    on_demand = lowest.get('uninterruptablePrice')
                    
                    # Determine availability
                    if on_demand and on_demand > 0:
                        availability = "available"
                    elif spot_price and spot_price > 0:
                        availability = "limited"
                    else:
                        availability = "unavailable"
                    
                    prices.append(GPUPrice(
                        provider="runpod",
                        gpu_name=gpu.get('displayName', 'Unknown'),
                        gpu_id=gpu.get('id', ''),
                        gpu_count=1,
                        vram_gb=gpu.get('memoryInGb', 0),
                        price_per_hour=on_demand or 0,
                        spot_price=spot_price,
                        availability=availability,
                    ))
                
        except Exception as e:
            logger.error(f"Failed to fetch RunPod prices: {e}")
        
        return prices


class LambdaPriceFetcher:
    """
    Fetches GPU prices from Lambda Labs API.
    
    Lambda has fixed pricing per instance type.
    """
    
    API_URL = "https://cloud.lambdalabs.com/api/v1"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def fetch_prices(self) -> List[GPUPrice]:
        """Fetch current GPU prices from Lambda Labs."""
        prices = []
        
        try:
            with httpx.Client(timeout=30) as client:
                # Get instance types
                response = client.get(
                    f"{self.API_URL}/instance-types",
                    headers={"Authorization": f"Bearer {self.api_key}"}
                )
                response.raise_for_status()
                data = response.json()
                
                instance_types = data.get('data', {})
                
                for instance_id, info in instance_types.items():
                    specs = info.get('instance_type', {}).get('specs', {})
                    gpu_info = specs.get('gpus', [{}])[0] if specs.get('gpus') else {}
                    
                    # Check availability
                    regions = info.get('regions_with_capacity_available', [])
                    availability = "available" if regions else "unavailable"
                    region = regions[0].get('name') if regions else None
                    
                    prices.append(GPUPrice(
                        provider="lambda",
                        gpu_name=gpu_info.get('name', instance_id),
                        gpu_id=instance_id,
                        gpu_count=gpu_info.get('count', 1),
                        vram_gb=gpu_info.get('memory_gib', 0),
                        price_per_hour=info.get('instance_type', {}).get('price_cents_per_hour', 0) / 100,
                        spot_price=None,  # Lambda doesn't have spot
                        region=region,
                        availability=availability,
                    ))
                
        except Exception as e:
            logger.error(f"Failed to fetch Lambda prices: {e}")
        
        return prices


class VastAIPriceFetcher:
    """
    Fetches GPU prices from Vast.ai marketplace.
    
    Vast.ai is a GPU marketplace with variable pricing.
    """
    
    API_URL = "https://console.vast.ai/api/v0"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def fetch_prices(self) -> List[GPUPrice]:
        """Fetch current GPU prices from Vast.ai."""
        prices = []
        
        try:
            with httpx.Client(timeout=30) as client:
                # Search for available offers
                response = client.get(
                    f"{self.API_URL}/bundles",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    params={
                        "q": json.dumps({
                            "verified": {"eq": True},
                            "rentable": {"eq": True},
                            "num_gpus": {"gte": 1},
                            "order": [["dph_total", "asc"]],
                            "limit": 100
                        })
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                offers = data.get('offers', [])
                
                # Group by GPU type and get cheapest
                gpu_prices: Dict[str, GPUPrice] = {}
                
                for offer in offers:
                    gpu_name = offer.get('gpu_name', 'Unknown')
                    
                    if gpu_name not in gpu_prices or offer.get('dph_total', 999) < gpu_prices[gpu_name].price_per_hour:
                        gpu_prices[gpu_name] = GPUPrice(
                            provider="vastai",
                            gpu_name=gpu_name,
                            gpu_id=str(offer.get('id', '')),
                            gpu_count=offer.get('num_gpus', 1),
                            vram_gb=int(offer.get('gpu_ram', 0) / 1024),  # MB to GB
                            price_per_hour=offer.get('dph_total', 0),
                            spot_price=offer.get('min_bid', None),
                            availability="available",
                        )
                
                prices = list(gpu_prices.values())
                
        except Exception as e:
            logger.error(f"Failed to fetch Vast.ai prices: {e}")
        
        return prices


class RealTimePriceFetcher:
    """
    Aggregates prices from all configured providers.
    
    Uses caching to minimize API calls while keeping prices fresh.
    """
    
    def __init__(self, config: Optional[MiniSkyConfig] = None, cache_ttl: int = 300):
        self.config = config or MiniSkyConfig()
        self.creds = CredentialManager(self.config)
        self.cache = PriceCache(ttl_seconds=cache_ttl)
        
        self._fetchers: Dict[str, Any] = {}
        self._init_fetchers()
    
    def _init_fetchers(self):
        """Initialize fetchers for configured providers."""
        # RunPod
        if self.creds.is_configured('runpod'):
            api_key = self.creds.get_api_key('runpod')
            if api_key:
                self._fetchers['runpod'] = RunPodPriceFetcher(api_key)
        
        # Lambda
        if self.creds.is_configured('lambda'):
            api_key = self.creds.get_api_key('lambda')
            if api_key:
                self._fetchers['lambda'] = LambdaPriceFetcher(api_key)
        
        # Vast.ai
        if self.creds.is_configured('vastai'):
            api_key = self.creds.get_api_key('vastai')
            if api_key:
                self._fetchers['vastai'] = VastAIPriceFetcher(api_key)
    
    def fetch_all(self, force_refresh: bool = False) -> List[GPUPrice]:
        """
        Fetch prices from all configured providers.
        
        Args:
            force_refresh: Bypass cache and fetch fresh prices
        
        Returns:
            List of GPUPrice from all providers
        """
        all_prices = []
        
        for provider, fetcher in self._fetchers.items():
            # Check cache first
            if not force_refresh:
                cached = self.cache.get(provider)
                if cached:
                    logger.debug(f"Using cached prices for {provider}")
                    all_prices.extend(cached)
                    continue
            
            # Fetch fresh prices
            logger.info(f"Fetching prices from {provider}...")
            prices = fetcher.fetch_prices()
            
            if prices:
                self.cache.set(provider, prices)
                all_prices.extend(prices)
        
        return all_prices
    
    def fetch_provider(self, provider: str, force_refresh: bool = False) -> List[GPUPrice]:
        """Fetch prices from a specific provider."""
        if provider not in self._fetchers:
            logger.warning(f"Provider {provider} not configured")
            return []
        
        if not force_refresh:
            cached = self.cache.get(provider)
            if cached:
                return cached
        
        prices = self._fetchers[provider].fetch_prices()
        if prices:
            self.cache.set(provider, prices)
        
        return prices
    
    def find_cheapest(
        self,
        gpu_name: Optional[str] = None,
        min_vram_gb: int = 0,
        gpu_count: int = 1,
        prefer_spot: bool = False,
        available_only: bool = True,
    ) -> Optional[GPUPrice]:
        """
        Find the cheapest GPU matching requirements.
        
        Args:
            gpu_name: Filter by GPU name (partial match)
            min_vram_gb: Minimum VRAM required
            gpu_count: Number of GPUs needed
            prefer_spot: Sort by spot price if available
            available_only: Only return available GPUs
        
        Returns:
            Cheapest matching GPUPrice or None
        """
        prices = self.fetch_all()
        
        # Filter
        filtered = []
        gpu_upper = (gpu_name or '').upper()
        
        for p in prices:
            # GPU name filter
            if gpu_upper and gpu_upper not in p.gpu_name.upper():
                continue
            
            # VRAM filter
            if p.vram_gb < min_vram_gb:
                continue
            
            # GPU count filter
            if p.gpu_count < gpu_count:
                continue
            
            # Availability filter
            if available_only and p.availability == "unavailable":
                continue
            
            filtered.append(p)
        
        if not filtered:
            return None
        
        # Sort by price
        def sort_key(p: GPUPrice) -> float:
            if prefer_spot and p.spot_price is not None:
                return p.spot_price
            return p.price_per_hour if p.price_per_hour is not None else 999
        
        filtered.sort(key=sort_key)
        return filtered[0]
    
    def compare_prices(
        self,
        gpu_name: Optional[str] = None,
        min_vram_gb: int = 0,
    ) -> List[GPUPrice]:
        """
        Get all prices for comparison, sorted by price.
        
        Args:
            gpu_name: Filter by GPU name
            min_vram_gb: Minimum VRAM
        
        Returns:
            Sorted list of GPUPrice
        """
        prices = self.fetch_all()
        
        # Filter
        filtered = []
        gpu_upper = (gpu_name or '').upper()
        
        for p in prices:
            if gpu_upper and gpu_upper not in p.gpu_name.upper():
                continue
            if p.vram_gb < min_vram_gb:
                continue
            filtered.append(p)
        
        # Sort by price
        filtered.sort(key=lambda p: p.price_per_hour if p.price_per_hour is not None else 999)
        return filtered


def get_live_prices(
    gpu_name: Optional[str] = None,
    config: Optional[MiniSkyConfig] = None,
) -> List[GPUPrice]:
    """
    Convenience function to get live GPU prices.
    
    Args:
        gpu_name: Optional GPU name filter
        config: MiniSky configuration
    
    Returns:
        List of GPUPrice sorted by price
    """
    fetcher = RealTimePriceFetcher(config)
    return fetcher.compare_prices(gpu_name=gpu_name)
