"""
Lambda Cloud provider implementation.

Manages GPU instances on Lambda Cloud via their REST API.

API Reference: https://docs.lambda.ai/public-cloud/cloud-api/
"""

import httpx
from typing import Dict, List, Any, Optional
from .base import BaseProvider, VMInfo, ProviderError
from ..credentials import CredentialManager


_API_BASE = "https://cloud.lambdalabs.com/api/v1"


class LambdaProvider(BaseProvider):
    """
    Lambda Cloud provider for GPU instance management.

    Features:
    - On-demand GPU instances
    - SSH key management
    - Instance type catalog with pricing
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._creds = CredentialManager()
        self._api_key = None
        self._client = None

    def _get_client(self) -> httpx.Client:
        """Get or create an authenticated HTTP client."""
        if self._client is None:
            self._api_key = self._creds.require_api_key('lambda')
            self._client = httpx.Client(
                base_url=_API_BASE,
                auth=(self._api_key, ''),
                headers={'Content-Type': 'application/json'},
                timeout=30.0,
            )
        return self._client

    def _resolve_instance_type(self, task: Any) -> str:
        """
        Resolve task resource requirements to a Lambda instance type.

        Args:
            task: Task with resource requirements

        Returns:
            Lambda instance type name

        Raises:
            ProviderError: If no matching instance type found
        """
        client = self._get_client()

        try:
            response = client.get('/instance-types')
            response.raise_for_status()
            data = response.json().get('data', {})
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda API error: {str(e)}")

        gpu_name = (task.resources.gpu or '').upper()
        gpu_count = task.resources.gpu_count

        candidates = []
        for type_name, type_info in data.items():
            specs = type_info.get('instance_type', {}).get('specs', {})
            type_gpu = specs.get('gpus', 0)
            type_gpu_desc = type_info.get('instance_type', {}).get('description', '')

            # Match GPU type name in description or type name
            if gpu_name and gpu_name.lower() not in type_name.lower() and gpu_name.lower() not in type_gpu_desc.lower():
                continue

            # Match GPU count
            if type_gpu < gpu_count:
                continue

            regions = type_info.get('regions_with_capacity_available', [])
            if regions:
                price = type_info.get('instance_type', {}).get('price_cents_per_hour', 0) / 100
                candidates.append({
                    'type_name': type_name,
                    'gpu_count': type_gpu,
                    'price': price,
                    'region': regions[0].get('name', 'unknown'),
                })

        if not candidates:
            raise ProviderError(
                f"No available Lambda instance type matching: "
                f"GPU={gpu_name or 'any'}, count={gpu_count}. "
                f"Check availability at https://lambda.ai/pricing"
            )

        # Pick cheapest available
        candidates.sort(key=lambda x: x['price'])
        return candidates[0]['type_name'], candidates[0]['region']

    def launch(self, task: Any) -> VMInfo:
        """
        Launch a new instance on Lambda Cloud.

        Args:
            task: Task definition

        Returns:
            VMInfo with instance details

        Raises:
            ProviderError: If launch fails
        """
        client = self._get_client()

        instance_type, region = self._resolve_instance_type(task)

        # Get SSH keys
        ssh_key_names = self._get_ssh_keys()
        if not ssh_key_names:
            raise ProviderError(
                "No SSH keys found on Lambda Cloud. "
                "Add one at https://cloud.lambdalabs.com/ssh-keys"
            )

        payload = {
            'region_name': region,
            'instance_type_name': instance_type,
            'ssh_key_names': ssh_key_names,
            'quantity': 1,
            'name': task.name,
        }

        try:
            response = client.post('/instance-operations/launch', json=payload)
            response.raise_for_status()
            data = response.json().get('data', {})
        except httpx.HTTPStatusError as e:
            error_msg = e.response.json().get('error', {}).get('message', e.response.text)
            raise ProviderError(f"Lambda launch error: {error_msg}")
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda connection error: {str(e)}")

        instance_ids = data.get('instance_ids', [])
        if not instance_ids:
            raise ProviderError("Lambda returned no instance IDs")

        instance_id = instance_ids[0]

        # Wait briefly for instance details
        import time
        time.sleep(5)

        # Get instance details
        instance = self._get_instance(instance_id)

        vm_info: VMInfo = {
            'vm_id': f"lambda-{instance_id}",
            'ip_address': instance.get('ip', 'pending'),
            'ssh_port': 22,
            'ssh_user': 'ubuntu',
            'status': 'running',
            'provider': 'lambda',
            'task_name': task.name,
            'instance_id': instance_id,
            'instance_type': instance_type,
            'region': region,
        }

        return vm_info

    def _get_instance(self, instance_id: str) -> dict:
        """Get instance details by ID."""
        client = self._get_client()

        try:
            response = client.get(f'/instances/{instance_id}')
            response.raise_for_status()
            return response.json().get('data', {})
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda API error: {str(e)}")

    def _get_ssh_keys(self) -> list:
        """Get list of SSH key names from Lambda account."""
        client = self._get_client()

        try:
            response = client.get('/ssh-keys')
            response.raise_for_status()
            keys = response.json().get('data', [])
            return [k.get('name') for k in keys if k.get('name')]
        except httpx.RequestError:
            return []

    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of a Lambda instance.

        Args:
            vm_id: VM identifier (format: lambda-{instance_id})

        Returns:
            VMInfo with current status
        """
        instance_id = vm_id.replace('lambda-', '', 1)
        instance = self._get_instance(instance_id)

        if not instance:
            raise ProviderError(f"Instance not found: {vm_id}")

        return {
            'vm_id': vm_id,
            'ip_address': instance.get('ip', 'pending'),
            'ssh_port': 22,
            'ssh_user': 'ubuntu',
            'status': instance.get('status', 'unknown'),
            'provider': 'lambda',
            'task_name': instance.get('name', 'unknown'),
            'instance_id': instance_id,
        }

    def terminate(self, vm_id: str) -> bool:
        """
        Terminate a Lambda instance.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_client()
        instance_id = vm_id.replace('lambda-', '', 1)

        try:
            response = client.post(
                '/instance-operations/terminate',
                json={'instance_ids': [instance_id]}
            )
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            error_msg = e.response.json().get('error', {}).get('message', e.response.text)
            raise ProviderError(f"Lambda terminate error: {error_msg}")
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda connection error: {str(e)}")

    def list_instances(self) -> List[VMInfo]:
        """
        List all Lambda instances.

        Returns:
            List of VMInfo dictionaries
        """
        client = self._get_client()

        try:
            response = client.get('/instances')
            response.raise_for_status()
            instances = response.json().get('data', [])
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda connection error: {str(e)}")

        result = []
        for inst in instances:
            result.append({
                'vm_id': f"lambda-{inst.get('id', 'unknown')}",
                'ip_address': inst.get('ip', 'pending'),
                'ssh_port': 22,
                'ssh_user': 'ubuntu',
                'status': inst.get('status', 'unknown'),
                'provider': 'lambda',
                'task_name': inst.get('name', 'unnamed'),
                'instance_id': inst.get('id', 'unknown'),
            })

        return result

    def get_gpu_catalog(self) -> List[Dict[str, Any]]:
        """
        Get available instance types and pricing from Lambda.

        Returns:
            List of GPU catalog entries
        """
        client = self._get_client()

        try:
            response = client.get('/instance-types')
            response.raise_for_status()
            data = response.json().get('data', {})
        except httpx.RequestError as e:
            raise ProviderError(f"Lambda catalog error: {str(e)}")

        catalog = []
        for type_name, type_info in data.items():
            specs = type_info.get('instance_type', {}).get('specs', {})
            regions = type_info.get('regions_with_capacity_available', [])
            price = type_info.get('instance_type', {}).get('price_cents_per_hour', 0) / 100

            catalog.append({
                'provider': 'lambda',
                'gpu_name': type_info.get('instance_type', {}).get('description', type_name),
                'instance_type': type_name,
                'gpu_count': specs.get('gpus', 0),
                'memory_gb': specs.get('ram_gib', 0),
                'vcpus': specs.get('vcpus', 0),
                'storage_gb': specs.get('storage_gib', 0),
                'price_per_hour': price,
                'available': len(regions) > 0,
                'regions': [r.get('name', '') for r in regions],
            })

        return catalog
