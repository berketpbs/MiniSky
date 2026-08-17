"""
Cost optimizer for MiniSky.

Automatically selects the cheapest provider/region/instance
combination that satisfies the task's resource requirements.
"""

from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table

from .config import MiniSkyConfig
from .credentials import CredentialManager

console = Console()


class OptimizerResult:
    """Result of an optimization query."""

    def __init__(
        self,
        provider: str,
        gpu_name: str,
        price_per_hour: float,
        spot_price: Optional[float] = None,
        region: Optional[str] = None,
        instance_type: Optional[str] = None,
        available: bool = True,
        price_is_estimate: bool = False,
    ):
        self.provider = provider
        self.gpu_name = gpu_name
        self.price_per_hour = price_per_hour
        self.spot_price = spot_price
        self.region = region
        self.instance_type = instance_type
        self.available = available
        self.price_is_estimate = price_is_estimate

    @property
    def effective_price(self) -> float:
        """Return spot price if available, otherwise on-demand."""
        if self.spot_price and self.spot_price > 0:
            return self.spot_price
        return self.price_per_hour

    def __repr__(self):
        return (
            f"OptimizerResult(provider={self.provider}, gpu={self.gpu_name}, "
            f"price=${self.price_per_hour:.2f}/hr, spot=${self.spot_price or 0:.2f}/hr)"
        )


class CostOptimizer:
    """
    Finds the cheapest cloud provider and instance type for a task.

    The optimizer queries all configured providers' catalogs,
    filters by the task's GPU/resource requirements, and ranks
    results by price (spot first if use_spot=True).
    """

    def __init__(self, config: Optional[MiniSkyConfig] = None):
        self._config = config or MiniSkyConfig()
        self._creds = CredentialManager(self._config)

    def find_best(self, task: Any, prefer_spot: bool = False) -> Optional[OptimizerResult]:
        """
        Find the single cheapest option for a task.

        Args:
            task: Task with resource requirements
            prefer_spot: Prefer spot instances even if not specified in task

        Returns:
            Best OptimizerResult or None if nothing available
        """
        candidates = self.find_all(task, prefer_spot=prefer_spot)
        if not candidates:
            return None
        return candidates[0]

    def find_all(self, task: Any, prefer_spot: bool = False) -> List[OptimizerResult]:
        """
        Find all matching options sorted by price (cheapest first).

        Args:
            task: Task with resource requirements
            prefer_spot: Prefer spot pricing for sorting

        Returns:
            Sorted list of OptimizerResult
        """
        use_spot = prefer_spot or getattr(task.resources, 'use_spot', False)
        gpu_name = getattr(task.resources, 'gpu', None)
        gpu_count = getattr(task.resources, 'gpu_count', 1)

        candidates = []

        # Query RunPod catalog
        if self._creds.is_configured('runpod'):
            candidates.extend(self._query_runpod(gpu_name, gpu_count))

        # Query Lambda catalog
        if self._creds.is_configured('lambda'):
            candidates.extend(self._query_lambda(gpu_name, gpu_count))

        # Query AWS catalog (static estimated pricing)
        if self._creds.is_aws_configured():
            candidates.extend(self._query_aws(gpu_name, gpu_count))

        # Query GCP catalog (static estimated pricing)
        if self._creds.is_gcp_configured():
            candidates.extend(self._query_gcp(gpu_name, gpu_count))

        # Always include mock (free)
        if gpu_name:
            candidates.append(OptimizerResult(
                provider='mock',
                gpu_name=f'Mock {gpu_name}',
                price_per_hour=0.00,
                spot_price=0.00,
                available=True,
            ))

        # Sort: available first, then by effective price
        def sort_key(r: OptimizerResult) -> Tuple[int, float]:
            avail = 0 if r.available else 1
            price = r.spot_price if (use_spot and r.spot_price is not None) else r.price_per_hour
            return (avail, price if price is not None else 999)

        candidates.sort(key=sort_key)
        return candidates

    def display_options(self, task: Any):
        """
        Display all optimization options as a Rich table.

        Args:
            task: Task with resource requirements
        """
        results = self.find_all(task)

        if not results:
            console.print("[yellow]No matching GPU options found across configured providers.[/yellow]")
            console.print("Run 'minisky check' to verify provider credentials.")
            return

        table = Table(title="Optimization Results (cheapest first)")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Provider", style="blue")
        table.add_column("GPU", style="cyan")
        table.add_column("On-Demand", style="yellow", justify="right")
        table.add_column("Spot", style="green", justify="right")
        table.add_column("Region", style="dim")
        table.add_column("Available", justify="center")

        any_estimated = False
        for i, r in enumerate(results, 1):
            avail = "[green]Yes[/green]" if r.available else "[red]No[/red]"
            estimate_suffix = "~" if r.price_is_estimate else ""
            if r.price_is_estimate:
                any_estimated = True
            price = f"${r.price_per_hour:.2f}/hr{estimate_suffix}" if r.price_per_hour else "-"
            spot = f"${r.spot_price:.2f}/hr" if r.spot_price else "-"
            row_style = "bold" if i == 1 else ""

            table.add_row(
                str(i),
                r.provider,
                r.gpu_name,
                price,
                spot,
                r.region or "-",
                avail,
                style=row_style,
            )

        console.print(table)
        if any_estimated:
            console.print("[dim]~ = static price estimate, not a live quote[/dim]")

        best = results[0]
        if best.available:
            console.print(
                f"\n[green]>[/green] Recommended: [bold]{best.provider}[/bold] "
                f"({best.gpu_name}) at ${best.effective_price:.2f}/hr"
            )

    def _query_runpod(self, gpu_name: Optional[str], gpu_count: int) -> List[OptimizerResult]:
        """Query RunPod catalog and filter by requirements."""
        try:
            from .providers.runpod import RunPodProvider
            provider = RunPodProvider()
            catalog = provider.get_gpu_catalog()
        except Exception:
            return []

        results = []
        gpu_upper = (gpu_name or '').upper()

        for entry in catalog:
            entry_name = entry.get('gpu_name', '').upper()

            # Filter by GPU name if specified
            if gpu_upper and gpu_upper not in entry_name:
                continue

            results.append(OptimizerResult(
                provider='runpod',
                gpu_name=entry.get('gpu_name', 'unknown'),
                price_per_hour=entry.get('price_per_hour', 0),
                spot_price=entry.get('spot_price'),
                available=entry.get('available', False),
            ))

        return results

    def _query_lambda(self, gpu_name: Optional[str], gpu_count: int) -> List[OptimizerResult]:
        """Query Lambda catalog and filter by requirements."""
        try:
            from .providers.lambda_cloud import LambdaProvider
            provider = LambdaProvider()
            catalog = provider.get_gpu_catalog()
        except Exception:
            return []

        results = []
        gpu_upper = (gpu_name or '').upper()

        for entry in catalog:
            entry_name = entry.get('gpu_name', '').upper()
            entry_count = entry.get('gpu_count', 0)

            # Filter by GPU name
            if gpu_upper and gpu_upper not in entry_name:
                continue

            # Filter by GPU count
            if entry_count < gpu_count:
                continue

            regions = entry.get('regions', [])
            region = regions[0] if regions else None

            results.append(OptimizerResult(
                provider='lambda',
                gpu_name=entry.get('gpu_name', 'unknown'),
                price_per_hour=entry.get('price_per_hour', 0),
                spot_price=None,  # Lambda doesn't have spot
                region=region,
                instance_type=entry.get('instance_type'),
                available=entry.get('available', False),
            ))

        return results

    def _query_aws(self, gpu_name: Optional[str], gpu_count: int) -> List[OptimizerResult]:
        """Query AWS's static GPU instance catalog and filter by requirements."""
        try:
            from .providers.aws import AWSProvider
            provider = AWSProvider()
            catalog = provider.get_gpu_catalog()
        except Exception:
            return []

        return self._filter_static_catalog('aws', catalog, gpu_name, gpu_count)

    def _query_gcp(self, gpu_name: Optional[str], gpu_count: int) -> List[OptimizerResult]:
        """Query GCP's static GPU machine-config catalog and filter by requirements."""
        try:
            from .providers.gcp import GCPProvider
            provider = GCPProvider()
            catalog = provider.get_gpu_catalog()
        except Exception:
            return []

        return self._filter_static_catalog('gcp', catalog, gpu_name, gpu_count)

    @staticmethod
    def _filter_static_catalog(
        provider_name: str,
        catalog: List[Dict[str, Any]],
        gpu_name: Optional[str],
        gpu_count: int,
    ) -> List[OptimizerResult]:
        """
        Shared filter/convert logic for AWS/GCP, whose catalogs are static
        (gpu_name, gpu_count) -> instance_type lookup tables rather than a
        live API, unlike RunPod/Lambda.
        """
        results = []
        gpu_upper = (gpu_name or '').upper()

        for entry in catalog:
            entry_name = entry.get('gpu_name', '').upper()
            entry_count = entry.get('gpu_count', 0)

            if gpu_upper and gpu_upper not in entry_name:
                continue
            if entry_count < gpu_count:
                continue

            results.append(OptimizerResult(
                provider=provider_name,
                gpu_name=entry.get('gpu_name', 'unknown'),
                price_per_hour=entry.get('price_per_hour') or 0,
                spot_price=None,
                instance_type=entry.get('instance_type'),
                available=entry.get('available', False),
                price_is_estimate=entry.get('price_is_estimate', False),
            ))

        return results
