"""
RunPod cloud provider implementation.

Manages GPU pod instances on RunPod via their REST API.
Supports on-demand and spot (interruptible) instances.

API Reference: https://docs.runpod.io/
"""

import time
import httpx
from typing import Dict, List, Any, Optional
from .base import BaseProvider, VMInfo, ProviderError
from ..credentials import CredentialManager


# RunPod GPU type mapping (MiniSky name -> RunPod gpuTypeId)
_GPU_TYPE_MAP = {
    'A100': 'NVIDIA A100 80GB PCIe',
    'A100-SXM': 'NVIDIA A100-SXM4-80GB',
    'H100': 'NVIDIA H100 80GB HBM3',
    'H100-SXM': 'NVIDIA H100-SXM5-80GB',
    'A40': 'NVIDIA A40',
    'RTX4090': 'NVIDIA GeForce RTX 4090',
    'RTX3090': 'NVIDIA GeForce RTX 3090',
    'RTX4080': 'NVIDIA GeForce RTX 4080',
    'L40': 'NVIDIA L40',
    'L40S': 'NVIDIA L40S',
    'RTX6000ADA': 'NVIDIA RTX 6000 Ada Generation',
    'V100': 'NVIDIA V100',
}

_API_BASE = "https://rest.runpod.io/v1"


class RunPodProvider(BaseProvider):
    """
    RunPod cloud provider for GPU instance management.

    Features:
    - On-demand and spot GPU pods
    - Docker-based instances with custom images
    - Persistent volume storage
    - GPU catalog and pricing queries
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._creds = CredentialManager()
        self._api_key = None
        self._client = None

    def _get_client(self) -> httpx.Client:
        """Get or create an authenticated HTTP client."""
        if self._client is None:
            self._api_key = self._creds.require_api_key('runpod')
            self._client = httpx.Client(
                base_url=_API_BASE,
                headers={
                    'Authorization': f'Bearer {self._api_key}',
                    'Content-Type': 'application/json',
                },
                timeout=30.0,
            )
        return self._client

    def _resolve_gpu_type(self, gpu_name: Optional[str]) -> Optional[str]:
        """Map MiniSky GPU name to RunPod gpuTypeId."""
        if gpu_name is None:
            return None
        gpu_upper = gpu_name.upper()
        if gpu_upper in _GPU_TYPE_MAP:
            return _GPU_TYPE_MAP[gpu_upper]
        # If not in map, pass through as-is (user might know the exact RunPod ID)
        return gpu_name

    def launch(self, task: Any) -> VMInfo:
        """
        Launch a new GPU pod on RunPod.

        Args:
            task: Task definition with resource requirements

        Returns:
            VMInfo with pod details

        Raises:
            ProviderError: If launch fails
        """
        client = self._get_client()

        gpu_type_id = self._resolve_gpu_type(task.resources.gpu)
        if gpu_type_id is None:
            raise ProviderError("GPU type is required for RunPod. Specify resources.gpu in your task YAML.")

        payload = {
            'name': task.name,
            'imageName': task.resources.image_id or 'runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04',
            'gpuTypeId': gpu_type_id,
            'gpuCount': task.resources.gpu_count,
            'containerDiskInGb': task.resources.disk_gb,
            'volumeInGb': max(task.resources.disk_gb // 2, 10),
            'supportPublicIp': True,
            'startSsh': True,
        }

        if task.resources.use_spot:
            payload['interruptible'] = True

        if task.resources.cpus:
            payload['vcpuCount'] = task.resources.cpus

        if task.ports:
            payload['ports'] = ','.join([f"{p}/http" for p in task.ports])

        if task.env:
            payload['env'] = {k: str(v) for k, v in task.env.items()}

        try:
            response = client.post('/pods', json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"RunPod API error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise ProviderError(f"RunPod connection error: {str(e)}")

        pod = data.get('pod', data)
        pod_id = pod.get('id', pod.get('podId', 'unknown'))

        vm_info: VMInfo = {
            'vm_id': f"runpod-{pod_id}",
            'ip_address': self._wait_for_ip(pod_id),
            'ssh_port': int(pod.get('sshPort', 22)),
            'ssh_user': 'root',
            'status': 'running',
            'provider': 'runpod',
            'task_name': task.name,
            'pod_id': pod_id,
            'gpu_type': task.resources.gpu,
            'gpu_count': task.resources.gpu_count,
            'spot': task.resources.use_spot,
        }

        return vm_info

    def _wait_for_ip(self, pod_id: str, timeout: int = 120) -> str:
        """
        Wait for a pod to get a public IP address.

        Args:
            pod_id: RunPod pod ID
            timeout: Maximum wait time in seconds

        Returns:
            Public IP address

        Raises:
            ProviderError: If timeout exceeded
        """
        client = self._get_client()
        start = time.time()

        while time.time() - start < timeout:
            try:
                response = client.get(f'/pods/{pod_id}')
                response.raise_for_status()
                pod = response.json().get('pod', response.json())

                ip = pod.get('publicIp') or pod.get('ip')
                if ip:
                    return ip

                status = pod.get('desiredStatus', pod.get('status', ''))
                if status in ('EXITED', 'TERMINATED', 'ERROR'):
                    raise ProviderError(f"Pod entered {status} state before getting IP")

            except httpx.RequestError:
                pass

            time.sleep(3)

        raise ProviderError(f"Timeout waiting for pod {pod_id} to get an IP address")

    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of a RunPod pod.

        Args:
            vm_id: VM identifier (format: runpod-{pod_id})

        Returns:
            VMInfo with current status

        Raises:
            ProviderError: If pod not found
        """
        client = self._get_client()
        pod_id = vm_id.replace('runpod-', '', 1)

        try:
            response = client.get(f'/pods/{pod_id}')
            response.raise_for_status()
            pod = response.json().get('pod', response.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ProviderError(f"Pod not found: {vm_id}")
            raise ProviderError(f"RunPod API error: {e.response.status_code}")
        except httpx.RequestError as e:
            raise ProviderError(f"RunPod connection error: {str(e)}")

        status_map = {
            'RUNNING': 'running',
            'EXITED': 'stopped',
            'TERMINATED': 'terminated',
            'CREATED': 'starting',
            'RESTARTING': 'starting',
        }
        pod_status = pod.get('desiredStatus', pod.get('status', 'unknown'))

        return {
            'vm_id': vm_id,
            'ip_address': pod.get('publicIp', pod.get('ip', 'pending')),
            'ssh_port': int(pod.get('sshPort', 22)),
            'ssh_user': 'root',
            'status': status_map.get(pod_status, pod_status.lower()),
            'provider': 'runpod',
            'task_name': pod.get('name', 'unknown'),
            'pod_id': pod_id,
        }

    def terminate(self, vm_id: str) -> bool:
        """
        Terminate a RunPod pod.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful

        Raises:
            ProviderError: If termination fails
        """
        client = self._get_client()
        pod_id = vm_id.replace('runpod-', '', 1)

        try:
            response = client.delete(f'/pods/{pod_id}')
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ProviderError(f"Pod not found: {vm_id}")
            raise ProviderError(f"RunPod API error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise ProviderError(f"RunPod connection error: {str(e)}")

    def stop(self, vm_id: str) -> bool:
        """
        Stop a RunPod pod (preserves disk).

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_client()
        pod_id = vm_id.replace('runpod-', '', 1)

        try:
            response = client.post(f'/pods/{pod_id}/stop')
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"RunPod stop error: {e.response.status_code} - {e.response.text}")

    def start(self, vm_id: str) -> bool:
        """
        Start a previously stopped RunPod pod.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_client()
        pod_id = vm_id.replace('runpod-', '', 1)

        try:
            response = client.post(f'/pods/{pod_id}/start')
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"RunPod start error: {e.response.status_code} - {e.response.text}")

    def list_instances(self) -> List[VMInfo]:
        """
        List all RunPod pods.

        Returns:
            List of VMInfo dictionaries
        """
        client = self._get_client()

        try:
            response = client.get('/pods')
            response.raise_for_status()
            pods = response.json().get('pods', response.json())
        except httpx.RequestError as e:
            raise ProviderError(f"RunPod connection error: {str(e)}")

        if not isinstance(pods, list):
            return []

        instances = []
        for pod in pods:
            pod_id = pod.get('id', pod.get('podId', 'unknown'))
            instances.append({
                'vm_id': f"runpod-{pod_id}",
                'ip_address': pod.get('publicIp', pod.get('ip', 'pending')),
                'ssh_port': int(pod.get('sshPort', 22)),
                'ssh_user': 'root',
                'status': pod.get('desiredStatus', 'unknown').lower(),
                'provider': 'runpod',
                'task_name': pod.get('name', 'unnamed'),
                'pod_id': pod_id,
            })

        return instances

    def get_gpu_catalog(self) -> List[Dict[str, Any]]:
        """
        Get available GPU types and pricing from RunPod.

        Returns:
            List of GPU catalog entries with pricing
        """
        client = self._get_client()

        try:
            response = client.get('/gpu-types')
            response.raise_for_status()
            data = response.json()
        except httpx.RequestError as e:
            raise ProviderError(f"RunPod catalog error: {str(e)}")

        gpu_types = data if isinstance(data, list) else data.get('gpuTypes', [])

        catalog = []
        for gpu in gpu_types:
            catalog.append({
                'provider': 'runpod',
                'gpu_name': gpu.get('displayName', gpu.get('id', 'unknown')),
                'gpu_id': gpu.get('id', 'unknown'),
                'memory_gb': gpu.get('memoryInGb', 0),
                'available': gpu.get('available', False),
                'price_per_hour': gpu.get('securePrice', gpu.get('communityPrice', 0)),
                'spot_price': gpu.get('communitySpotPrice', gpu.get('spotPrice', None)),
            })

        return catalog
