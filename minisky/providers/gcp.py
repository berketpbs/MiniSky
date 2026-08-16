"""
GCP Compute Engine provider implementation.

Manages GPU instances on Google Compute Engine via the
google-cloud-compute client library.

Credentials are resolved through google-auth's standard chain
(GOOGLE_APPLICATION_CREDENTIALS env var, `gcloud auth application-default
login`, or the GCE metadata server when running on GCP) unless explicitly
overridden in MiniSky's config under providers.gcp.*. This deliberately
mirrors how `gcloud`/every other Google Cloud client library already
resolves credentials, instead of inventing a MiniSky-specific format.
"""

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.cloud import compute_v1
from google.api_core.exceptions import GoogleAPIError, NotFound
from google.oauth2 import service_account

from .base import BaseProvider, ProviderError, VMInfo
from ..config import MiniSkyConfig

# (gpu_name, gpu_count) -> (machine_type, accelerator_type, accelerator_count).
# A100 uses the a2 machine family, which bundles its GPUs into the machine
# type itself (no separate guest_accelerators attachment) - everything else
# needs a standard/g2 machine type plus an explicit accelerator attachment.
# Only combinations GCP actually offers are listed; anything else is
# rejected with a clear error rather than guessing.
_GPU_CONFIG_MAP = {
    ("T4", 1): ("n1-standard-4", "nvidia-tesla-t4", 1),
    ("T4", 4): ("n1-standard-16", "nvidia-tesla-t4", 4),
    ("V100", 1): ("n1-standard-8", "nvidia-tesla-v100", 1),
    ("V100", 8): ("n1-standard-64", "nvidia-tesla-v100", 8),
    ("P100", 1): ("n1-standard-8", "nvidia-tesla-p100", 1),
    ("L4", 1): ("g2-standard-4", "nvidia-l4", 1),
    ("L4", 8): ("g2-standard-96", "nvidia-l4", 8),
    ("A100", 1): ("a2-highgpu-1g", None, 0),
    ("A100", 2): ("a2-highgpu-2g", None, 0),
    ("A100", 4): ("a2-highgpu-4g", None, 0),
    ("A100", 8): ("a2-highgpu-8g", None, 0),
}

# Static, approximate on-demand pricing (USD/hr, us-central1) for the GPU
# catalog view - GCP doesn't offer a simple public pricing endpoint the way
# RunPod/Lambda do. Indicative only, intentionally NOT used for billing.
_APPROX_PRICE_PER_HOUR = {
    ("T4", 1): 0.35 + 0.19,
    ("T4", 4): 0.76 + 4 * 0.19,
    ("V100", 1): 0.38 + 2.48,
    ("V100", 8): 3.04 + 8 * 2.48,
    ("P100", 1): 0.38 + 1.46,
    ("L4", 1): 0.20 + 0.35,
    ("L4", 8): 4.56 + 8 * 0.35,
    ("A100", 1): 3.67,
    ("A100", 2): 7.34,
    ("A100", 4): 14.69,
    ("A100", 8): 29.39,
}

_DEFAULT_ZONE = "us-central1-a"
_DEFAULT_IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
_SSH_USER = "ubuntu"

# MiniSky labels every instance it launches so list_instances() only ever
# returns VMs MiniSky itself manages, never the project's unrelated fleet.
# GCE label keys/values must be lowercase [a-z0-9_-].
_MANAGED_LABEL_KEY = "managed-by"
_MANAGED_LABEL_VALUE = "minisky"

_STATE_MAP = {
    "PROVISIONING": "starting",
    "STAGING": "starting",
    "RUNNING": "running",
    "STOPPING": "stopping",
    # GCE's own terminology: a stopped instance's status is "TERMINATED".
    # There is no separate "stopped" status - a truly deleted instance
    # just doesn't exist (404) instead.
    "TERMINATED": "stopped",
    "SUSPENDING": "stopping",
    "SUSPENDED": "stopped",
}


class GCPProvider(BaseProvider):
    """
    GCP Compute Engine provider for GPU instance management.

    Features:
    - On-demand and spot (preemptible) GPU instances
    - Persistent-disk-backed boot disk (stop preserves disk, matching
      native GCE stop/start semantics)
    - Instances are labeled and filtered so MiniSky never touches VMs it
      didn't launch
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._msconfig = MiniSkyConfig()
        self._instances_client = None
        self._zone = None

    def _credentials_kwargs(self) -> Dict[str, Any]:
        """Build client kwargs from config, falling back to google-auth's
        own default credential chain (GOOGLE_APPLICATION_CREDENTIALS,
        `gcloud auth application-default login`, or the GCE metadata
        server) when no explicit service account is configured."""
        creds_path = self._msconfig.get("providers.gcp.credentials_path")
        if creds_path:
            try:
                creds = service_account.Credentials.from_service_account_file(creds_path)
            except (OSError, ValueError) as e:
                raise ProviderError(f"GCP credential error: could not load {creds_path}: {str(e)}")
            return {"credentials": creds}
        return {}

    def _get_instances_client(self):
        """Get or create the Compute Engine instances client."""
        if self._instances_client is None:
            self._instances_client = compute_v1.InstancesClient(**self._credentials_kwargs())
        return self._instances_client

    def _project(self) -> str:
        project = self._msconfig.get("providers.gcp.project")
        if not project:
            raise ProviderError(
                "GCP project is required. Set it via: "
                "minisky config set providers.gcp.project YOUR_PROJECT_ID"
            )
        return project

    def _get_zone(self) -> str:
        self._zone = self._msconfig.get("providers.gcp.zone") or _DEFAULT_ZONE
        return self._zone

    def _resolve_machine_config(self, gpu_name: Optional[str], gpu_count: int):
        """Map a MiniSky GPU request to a concrete (machine_type,
        accelerator_type, accelerator_count)."""
        if not gpu_name:
            raise ProviderError("GPU type is required for GCP. Specify resources.gpu in your task YAML.")

        key = (gpu_name.upper(), gpu_count)
        if key not in _GPU_CONFIG_MAP:
            supported = ", ".join(f"{g} x{c}" for g, c in sorted(_GPU_CONFIG_MAP))
            raise ProviderError(
                f"No GCP machine config for {gpu_name} x{gpu_count}. "
                f"Supported combinations: {supported}"
            )
        return _GPU_CONFIG_MAP[key]

    def _ssh_metadata_items(self) -> List["compute_v1.Items"]:
        """Build instance metadata injecting an SSH public key, so the
        launched VM is actually reachable. Uses
        providers.gcp.ssh_public_key_path if configured, else falls back
        to ~/.ssh/id_rsa.pub if it exists. Without either, the instance
        boots with no SSH access configured by MiniSky."""
        pubkey_path = self._msconfig.get("providers.gcp.ssh_public_key_path")
        if not pubkey_path:
            default_path = Path.home() / ".ssh" / "id_rsa.pub"
            if default_path.exists():
                pubkey_path = str(default_path)

        if not pubkey_path or not Path(pubkey_path).exists():
            return []

        pubkey = Path(pubkey_path).read_text().strip()
        return [compute_v1.Items(key="ssh-keys", value=f"{_SSH_USER}:{pubkey}")]

    def launch(self, task: Any) -> VMInfo:
        """
        Launch a new GPU Compute Engine instance.

        Args:
            task: Task definition with resource requirements

        Returns:
            VMInfo with instance details

        Raises:
            ProviderError: If launch fails
        """
        # Validate task/config args before touching the network -
        # compute_v1.InstancesClient() eagerly resolves credentials at
        # construction time (unlike e.g. boto3, which is lazy), so a bad
        # GPU request would otherwise fail with a confusing credentials
        # error instead of the actual validation problem.
        project = self._project()
        zone = self._get_zone()
        machine_type, accel_type, accel_count = self._resolve_machine_config(
            task.resources.gpu, task.resources.gpu_count
        )
        image = task.resources.image_id or _DEFAULT_IMAGE
        instance_name = f"minisky-{uuid.uuid4().hex[:8]}"

        client = self._get_instances_client()

        instance = compute_v1.Instance(
            name=instance_name,
            machine_type=f"zones/{zone}/machineTypes/{machine_type}",
            disks=[compute_v1.AttachedDisk(
                auto_delete=True,
                boot=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=image,
                    disk_size_gb=task.resources.disk_gb,
                ),
            )],
            network_interfaces=[compute_v1.NetworkInterface(
                access_configs=[compute_v1.AccessConfig(
                    name="External NAT",
                    type_="ONE_TO_ONE_NAT",
                )],
            )],
            labels={_MANAGED_LABEL_KEY: _MANAGED_LABEL_VALUE},
            metadata=compute_v1.Metadata(items=self._ssh_metadata_items()),
        )

        if accel_type and accel_count:
            instance.guest_accelerators = [compute_v1.AcceleratorConfig(
                accelerator_type=f"zones/{zone}/acceleratorTypes/{accel_type}",
                accelerator_count=accel_count,
            )]
            # GPU instances can't live-migrate - required whenever GPUs are attached.
            instance.scheduling = compute_v1.Scheduling(on_host_maintenance="TERMINATE")

        if task.resources.use_spot:
            instance.scheduling = compute_v1.Scheduling(
                provisioning_model="SPOT",
                on_host_maintenance="TERMINATE",
                instance_termination_action="STOP",
            )

        try:
            operation = client.insert(project=project, zone=zone, instance_resource=instance)
            operation.result(timeout=300)
            if operation.error_code:
                raise ProviderError(f"GCP launch error: {operation.error_message}")
        except GoogleAPIError as e:
            raise ProviderError(f"GCP launch error: {str(e)}")

        vm_info: VMInfo = {
            "vm_id": f"gcp-{instance_name}",
            "ip_address": self._wait_for_ip(client, project, zone, instance_name),
            "ssh_port": 22,
            "ssh_user": _SSH_USER,
            "status": "running",
            "provider": "gcp",
            "task_name": task.name,
            "instance_id": instance_name,
            "machine_type": machine_type,
            "zone": zone,
            "spot": task.resources.use_spot,
        }

        return vm_info

    def _describe(self, client, project: str, zone: str, instance_name: str):
        """Fetch the raw instance description, or raise ProviderError."""
        try:
            return client.get(project=project, zone=zone, instance=instance_name)
        except NotFound:
            raise ProviderError(f"Instance not found: {instance_name}")
        except GoogleAPIError as e:
            raise ProviderError(f"GCP API error: {str(e)}")

    def _wait_for_ip(self, client, project: str, zone: str, instance_name: str, timeout: int = 180) -> str:
        """
        Wait for an instance to enter RUNNING and get a public IP.

        Raises:
            ProviderError: If timeout exceeded or the instance fails to boot
        """
        start = time.time()

        while time.time() - start < timeout:
            try:
                instance = self._describe(client, project, zone, instance_name)
            except ProviderError:
                time.sleep(5)
                continue

            if instance.status in ("STOPPING", "TERMINATED", "SUSPENDED", "SUSPENDING"):
                raise ProviderError(f"Instance entered '{instance.status}' state before getting an IP")

            for interface in instance.network_interfaces:
                for access_config in interface.access_configs:
                    if access_config.nat_i_p:
                        return access_config.nat_i_p

            time.sleep(5)

        raise ProviderError(f"Timeout waiting for instance {instance_name} to get a public IP")

    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of a Compute Engine instance.

        Args:
            vm_id: VM identifier (format: gcp-{instance_name})

        Returns:
            VMInfo with current status

        Raises:
            ProviderError: If instance not found
        """
        client = self._get_instances_client()
        project = self._project()
        zone = self._get_zone()
        instance_name = vm_id.replace("gcp-", "", 1)
        instance = self._describe(client, project, zone, instance_name)

        ip = "pending"
        for interface in instance.network_interfaces:
            for access_config in interface.access_configs:
                if access_config.nat_i_p:
                    ip = access_config.nat_i_p

        return {
            "vm_id": vm_id,
            "ip_address": ip,
            "ssh_port": 22,
            "ssh_user": _SSH_USER,
            "status": _STATE_MAP.get(instance.status, instance.status.lower()),
            "provider": "gcp",
            "task_name": instance.name,
            "instance_id": instance_name,
            "machine_type": instance.machine_type.rsplit("/", 1)[-1],
        }

    def terminate(self, vm_id: str) -> bool:
        """
        Terminate (delete) a Compute Engine instance.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful

        Raises:
            ProviderError: If termination fails
        """
        client = self._get_instances_client()
        instance_name = vm_id.replace("gcp-", "", 1)

        try:
            operation = client.delete(project=self._project(), zone=self._get_zone(), instance=instance_name)
            operation.result(timeout=180)
            return True
        except NotFound:
            raise ProviderError(f"Instance not found: {vm_id}")
        except GoogleAPIError as e:
            raise ProviderError(f"GCP terminate error: {str(e)}")

    def stop(self, vm_id: str) -> bool:
        """
        Stop a Compute Engine instance. The boot persistent disk is
        preserved, matching native GCE stop/start semantics.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_instances_client()
        instance_name = vm_id.replace("gcp-", "", 1)

        try:
            operation = client.stop(project=self._project(), zone=self._get_zone(), instance=instance_name)
            operation.result(timeout=180)
            return True
        except GoogleAPIError as e:
            raise ProviderError(f"GCP stop error: {str(e)}")

    def start(self, vm_id: str) -> bool:
        """
        Start a previously stopped Compute Engine instance.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_instances_client()
        instance_name = vm_id.replace("gcp-", "", 1)

        try:
            operation = client.start(project=self._project(), zone=self._get_zone(), instance=instance_name)
            operation.result(timeout=180)
            return True
        except GoogleAPIError as e:
            raise ProviderError(f"GCP start error: {str(e)}")

    def list_instances(self) -> List[VMInfo]:
        """
        List all Compute Engine instances MiniSky has launched (labeled
        managed-by=minisky) in the configured zone.

        Returns:
            List of VMInfo dictionaries
        """
        client = self._get_instances_client()
        project = self._project()
        zone = self._get_zone()

        try:
            results = client.list(
                project=project,
                zone=zone,
                filter=f"labels.{_MANAGED_LABEL_KEY} = {_MANAGED_LABEL_VALUE}",
            )
        except GoogleAPIError as e:
            raise ProviderError(f"GCP list error: {str(e)}")

        instances = []
        for instance in results:
            ip = "pending"
            for interface in instance.network_interfaces:
                for access_config in interface.access_configs:
                    if access_config.nat_i_p:
                        ip = access_config.nat_i_p

            instances.append({
                "vm_id": f"gcp-{instance.name}",
                "ip_address": ip,
                "ssh_port": 22,
                "ssh_user": _SSH_USER,
                "status": _STATE_MAP.get(instance.status, instance.status.lower()),
                "provider": "gcp",
                "task_name": instance.name,
                "instance_id": instance.name,
                "machine_type": instance.machine_type.rsplit("/", 1)[-1],
            })

        return instances

    def get_gpu_catalog(self) -> List[Dict[str, Any]]:
        """
        Get available GPU machine configs with approximate on-demand pricing.

        Note: prices are static estimates (us-central1 on-demand), not a
        live quote - GCP doesn't offer a simple public pricing endpoint
        the way RunPod/Lambda do.

        Returns:
            List of GPU catalog entries
        """
        catalog = []
        for (gpu_name, gpu_count), (machine_type, accel_type, accel_count) in _GPU_CONFIG_MAP.items():
            catalog.append({
                "provider": "gcp",
                "gpu_name": gpu_name,
                "instance_type": machine_type,
                "gpu_count": gpu_count,
                "price_per_hour": _APPROX_PRICE_PER_HOUR.get((gpu_name, gpu_count)),
                "price_is_estimate": True,
                "available": True,
            })
        return catalog
