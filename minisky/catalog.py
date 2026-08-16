"""
GPU catalog for comparing pricing and availability across providers.

Queries each configured provider's catalog endpoint and presents
a unified view of available GPU instances with pricing.
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from rich.console import Console
from rich.table import Table

from .config import MiniSkyConfig
from .credentials import CredentialManager

console = Console()


class GPUCatalog:
    """
    Unified GPU catalog across all configured providers.

    Features:
    - Query GPU availability and pricing from RunPod and Lambda
    - Local cache to avoid repeated API calls
    - Rich table display for CLI
    - Filter by GPU type, price range, and availability
    """

    def __init__(self, config: Optional[MiniSkyConfig] = None):
        self._config = config or MiniSkyConfig()
        self._creds = CredentialManager(self._config)
        self._cache_path = self._config.config_dir / 'gpu_cache.json'
        self._cache_ttl_seconds = 300  # 5 minutes

    def fetch_all(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch GPU catalog from all configured providers.

        Args:
            force_refresh: Skip cache and fetch fresh data

        Returns:
            List of GPU catalog entries
        """
        # Check cache
        if not force_refresh:
            cached = self._read_cache()
            if cached is not None:
                return cached

        catalog = []

        # RunPod
        if self._creds.is_configured('runpod'):
            try:
                from .providers.runpod import RunPodProvider
                provider = RunPodProvider()
                entries = provider.get_gpu_catalog()
                catalog.extend(entries)
            except Exception as e:
                console.print(f"[yellow]RunPod catalog unavailable: {e}[/yellow]")

        # Lambda
        if self._creds.is_configured('lambda'):
            try:
                from .providers.lambda_cloud import LambdaProvider
                provider = LambdaProvider()
                entries = provider.get_gpu_catalog()
                catalog.extend(entries)
            except Exception as e:
                console.print(f"[yellow]Lambda catalog unavailable: {e}[/yellow]")

        # AWS
        if self._creds.is_aws_configured():
            try:
                from .providers.aws import AWSProvider
                provider = AWSProvider()
                entries = provider.get_gpu_catalog()
                catalog.extend(entries)
            except Exception as e:
                console.print(f"[yellow]AWS catalog unavailable: {e}[/yellow]")

        # GCP
        if self._creds.is_gcp_configured():
            try:
                from .providers.gcp import GCPProvider
                provider = GCPProvider()
                entries = provider.get_gpu_catalog()
                catalog.extend(entries)
            except Exception as e:
                console.print(f"[yellow]GCP catalog unavailable: {e}[/yellow]")

        # Add mock entries for reference
        catalog.extend(self._mock_catalog())

        # Sort by price
        catalog.sort(key=lambda x: x.get('price_per_hour', 999))

        # Cache results
        self._write_cache(catalog)

        return catalog

    def display(
        self,
        gpu_filter: Optional[str] = None,
        available_only: bool = False,
    ):
        """
        Display GPU catalog as a Rich table.

        Args:
            gpu_filter: Filter by GPU name (case-insensitive substring match)
            available_only: Only show available GPUs
        """
        entries = self.fetch_all()

        if gpu_filter:
            gpu_filter_upper = gpu_filter.upper()
            entries = [e for e in entries if gpu_filter_upper in e.get('gpu_name', '').upper()]

        if available_only:
            entries = [e for e in entries if e.get('available', False)]

        if not entries:
            console.print("[yellow]No matching GPUs found[/yellow]")
            return

        table = Table(title="GPU Catalog")
        table.add_column("Provider", style="blue", no_wrap=True)
        table.add_column("GPU", style="cyan")
        table.add_column("VRAM", style="green", justify="right")
        table.add_column("$/hr", style="yellow", justify="right")
        table.add_column("Spot $/hr", style="magenta", justify="right")
        table.add_column("Available", justify="center")

        for entry in entries:
            available_str = "[green]Yes[/green]" if entry.get('available') else "[red]No[/red]"
            price = f"${entry['price_per_hour']:.2f}" if entry.get('price_per_hour') is not None else "-"
            spot = f"${entry['spot_price']:.2f}" if entry.get('spot_price') is not None else "-"
            vram = f"{entry['memory_gb']} GB" if entry.get('memory_gb') is not None else "-"

            table.add_row(
                entry.get('provider', '-'),
                entry.get('gpu_name', '-'),
                vram,
                price,
                spot,
                available_str,
            )

        console.print(table)

    def _mock_catalog(self) -> List[Dict[str, Any]]:
        """Return mock GPU entries for testing reference."""
        return [
            {
                'provider': 'mock',
                'gpu_name': 'Mock A100 80GB',
                'memory_gb': 80,
                'available': True,
                'price_per_hour': 0.00,
                'spot_price': 0.00,
            },
            {
                'provider': 'mock',
                'gpu_name': 'Mock H100 80GB',
                'memory_gb': 80,
                'available': True,
                'price_per_hour': 0.00,
                'spot_price': 0.00,
            },
        ]

    def _read_cache(self) -> Optional[List[Dict[str, Any]]]:
        """Read catalog from local cache if still valid."""
        if not self._cache_path.exists():
            return None

        try:
            with open(self._cache_path, 'r') as f:
                data = json.load(f)

            cached_at_str = data.get('cached_at')
            if not cached_at_str:
                return None
            cached_at = datetime.fromisoformat(cached_at_str)
            age = (datetime.now() - cached_at).total_seconds()

            if age > self._cache_ttl_seconds:
                return None

            return data.get('entries', [])
        except (json.JSONDecodeError, ValueError):
            return None

    def _write_cache(self, entries: List[Dict[str, Any]]):
        """Write catalog to local cache."""
        data = {
            'cached_at': datetime.now().isoformat(),
            'entries': entries,
        }
        try:
            with open(self._cache_path, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass  # Cache write failure is non-critical
