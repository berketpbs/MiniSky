"""
AWS EC2 provider implementation.

Manages GPU instances on Amazon EC2 via boto3.

Credentials are resolved through boto3's standard chain (environment
variables, ~/.aws/credentials, an IAM role, etc.) unless explicitly
overridden in MiniSky's config under providers.aws.*. This deliberately
mirrors how `aws configure` / the AWS CLI already work, instead of
inventing a MiniSky-specific credential format for AWS.
"""

import time
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from .base import BaseProvider, ProviderError, VMInfo
from ..config import MiniSkyConfig

# GPU type + count -> EC2 instance type. Only combinations AWS actually
# offers as a single instance are listed; anything else is rejected with
# a clear error rather than guessing.
_GPU_INSTANCE_MAP = {
    ("A100", 8): "p4d.24xlarge",
    ("V100", 1): "p3.2xlarge",
    ("V100", 4): "p3.8xlarge",
    ("V100", 8): "p3.16xlarge",
    ("T4", 1): "g4dn.xlarge",
    ("T4", 4): "g4dn.12xlarge",
    ("A10G", 1): "g5.xlarge",
    ("A10G", 4): "g5.12xlarge",
    ("A10G", 8): "g5.48xlarge",
    ("K80", 1): "p2.xlarge",
}

# Static, approximate on-demand pricing (USD/hr, us-east-1) for the GPU
# catalog view. AWS doesn't expose a simple pricing endpoint the way
# RunPod/Lambda do - real-time prices require the Price List API. These
# are indicative only and intentionally NOT used for billing.
_APPROX_PRICE_PER_HOUR = {
    "p4d.24xlarge": 32.77,
    "p3.2xlarge": 3.06,
    "p3.8xlarge": 12.24,
    "p3.16xlarge": 24.48,
    "g4dn.xlarge": 0.526,
    "g4dn.12xlarge": 3.912,
    "g5.xlarge": 1.006,
    "g5.12xlarge": 5.672,
    "g5.48xlarge": 16.288,
    "p2.xlarge": 0.90,
}

_DEFAULT_REGION = "us-east-1"

# MiniSky tags every instance it launches with this so list_instances()
# only ever returns VMs MiniSky itself manages, never the account's
# unrelated EC2 fleet.
_MANAGED_TAG = {"Key": "ManagedBy", "Value": "minisky"}

_STATE_MAP = {
    "pending": "starting",
    "running": "running",
    "stopping": "stopping",
    "stopped": "stopped",
    "shutting-down": "terminating",
    "terminated": "terminated",
}


class AWSProvider(BaseProvider):
    """
    AWS EC2 provider for GPU instance management.

    Features:
    - On-demand and spot GPU instances (p3/p4d/g4dn/g5/p2 families)
    - EBS-backed root volume (stop preserves disk, matching native EC2 semantics)
    - Instances are tagged and filtered so MiniSky never touches VMs it
      didn't launch
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._msconfig = MiniSkyConfig()
        self._client = None
        self._region = None

    def _session_kwargs(self) -> Dict[str, Any]:
        """Build boto3 client kwargs from config, falling back to boto3's
        own default credential chain (env vars, ~/.aws/credentials, IAM
        role) when no explicit key pair is configured."""
        self._region = self._msconfig.get("providers.aws.region") or _DEFAULT_REGION
        kwargs: Dict[str, Any] = {"region_name": self._region}

        access_key = self._msconfig.get("providers.aws.access_key_id")
        secret_key = self._msconfig.get("providers.aws.secret_access_key")
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        return kwargs

    def _get_client(self):
        """Get or create a boto3 EC2 client."""
        if self._client is None:
            try:
                self._client = boto3.client("ec2", **self._session_kwargs())
            except (BotoCoreError, NoCredentialsError) as e:
                raise ProviderError(f"AWS credential error: {str(e)}")
        return self._client

    def _resolve_instance_type(self, gpu_name: Optional[str], gpu_count: int) -> str:
        """Map a MiniSky GPU request to a concrete EC2 instance type."""
        if not gpu_name:
            raise ProviderError("GPU type is required for AWS. Specify resources.gpu in your task YAML.")

        key = (gpu_name.upper(), gpu_count)
        if key not in _GPU_INSTANCE_MAP:
            supported = ", ".join(f"{g} x{c}" for g, c in sorted(_GPU_INSTANCE_MAP))
            raise ProviderError(
                f"No AWS instance type for {gpu_name} x{gpu_count}. "
                f"Supported combinations: {supported}"
            )
        return _GPU_INSTANCE_MAP[key]

    def _resolve_ami(self, image_id: Optional[str]) -> str:
        """
        Resolve the AMI to boot. Uses the task's explicit image_id if
        given, otherwise looks up the latest Ubuntu 22.04 AMI for the
        current region via the public SSM parameter Canonical maintains
        (AMI IDs are region-specific, so hardcoding one would silently
        break in every region but one).
        """
        if image_id:
            return image_id

        ssm = boto3.client("ssm", **self._session_kwargs())
        param_name = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
        try:
            response = ssm.get_parameter(Name=param_name)
            return response["Parameter"]["Value"]
        except (ClientError, BotoCoreError) as e:
            raise ProviderError(
                f"Could not resolve a default AMI for region {self._region}: {str(e)}. "
                f"Specify resources.image_id explicitly in your task YAML."
            )

    def launch(self, task: Any) -> VMInfo:
        """
        Launch a new GPU EC2 instance.

        Args:
            task: Task definition with resource requirements

        Returns:
            VMInfo with instance details

        Raises:
            ProviderError: If launch fails
        """
        client = self._get_client()

        instance_type = self._resolve_instance_type(task.resources.gpu, task.resources.gpu_count)
        ami_id = self._resolve_ami(task.resources.image_id)

        run_kwargs: Dict[str, Any] = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "BlockDeviceMappings": [{
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": task.resources.disk_gb, "VolumeType": "gp3"},
            }],
            "TagSpecifications": [{
                "ResourceType": "instance",
                "Tags": [_MANAGED_TAG, {"Key": "Name", "Value": task.name}],
            }],
        }

        key_name = self._msconfig.get("providers.aws.key_name")
        if key_name:
            run_kwargs["KeyName"] = key_name

        security_group_id = self._msconfig.get("providers.aws.security_group_id")
        if security_group_id:
            run_kwargs["SecurityGroupIds"] = [security_group_id]

        subnet_id = self._msconfig.get("providers.aws.subnet_id")
        if subnet_id:
            run_kwargs["SubnetId"] = subnet_id

        if task.resources.use_spot:
            run_kwargs["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": {"SpotInstanceType": "one-time"},
            }

        try:
            response = client.run_instances(**run_kwargs)
        except ClientError as e:
            raise ProviderError(f"AWS launch error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

        instance_id = response["Instances"][0]["InstanceId"]

        vm_info: VMInfo = {
            "vm_id": f"aws-{instance_id}",
            "ip_address": self._wait_for_ip(client, instance_id),
            "ssh_port": 22,
            "ssh_user": "ubuntu",
            "status": "running",
            "provider": "aws",
            "task_name": task.name,
            "instance_id": instance_id,
            "instance_type": instance_type,
            "region": self._region,
            "spot": task.resources.use_spot,
        }

        return vm_info

    def _describe(self, client, instance_id: str) -> dict:
        """Fetch the raw instance description, or raise ProviderError."""
        try:
            response = client.describe_instances(InstanceIds=[instance_id])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidInstanceID.NotFound":
                raise ProviderError(f"Instance not found: {instance_id}")
            raise ProviderError(f"AWS API error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise ProviderError(f"Instance not found: {instance_id}")

        return reservations[0]["Instances"][0]

    def _wait_for_ip(self, client, instance_id: str, timeout: int = 180) -> str:
        """
        Wait for an instance to enter 'running' and get a public IP.

        Args:
            instance_id: EC2 instance ID
            timeout: Maximum wait time in seconds

        Returns:
            Public IP address

        Raises:
            ProviderError: If timeout exceeded or the instance fails to boot
        """
        start = time.time()

        while time.time() - start < timeout:
            try:
                instance = self._describe(client, instance_id)
            except ProviderError:
                time.sleep(5)
                continue

            state = instance.get("State", {}).get("Name", "")
            if state in ("shutting-down", "terminated"):
                raise ProviderError(f"Instance entered '{state}' state before getting an IP")

            ip = instance.get("PublicIpAddress")
            if ip:
                return ip

            time.sleep(5)

        raise ProviderError(f"Timeout waiting for instance {instance_id} to get a public IP")

    def status(self, vm_id: str) -> VMInfo:
        """
        Get current status of an EC2 instance.

        Args:
            vm_id: VM identifier (format: aws-{instance_id})

        Returns:
            VMInfo with current status

        Raises:
            ProviderError: If instance not found
        """
        client = self._get_client()
        instance_id = vm_id.replace("aws-", "", 1)
        instance = self._describe(client, instance_id)

        state = instance.get("State", {}).get("Name", "unknown")
        name_tag = next(
            (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
            "unknown",
        )

        return {
            "vm_id": vm_id,
            "ip_address": instance.get("PublicIpAddress", "pending"),
            "ssh_port": 22,
            "ssh_user": "ubuntu",
            "status": _STATE_MAP.get(state, state),
            "provider": "aws",
            "task_name": name_tag,
            "instance_id": instance_id,
            "instance_type": instance.get("InstanceType", "unknown"),
        }

    def terminate(self, vm_id: str) -> bool:
        """
        Terminate an EC2 instance.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful

        Raises:
            ProviderError: If termination fails
        """
        client = self._get_client()
        instance_id = vm_id.replace("aws-", "", 1)

        try:
            client.terminate_instances(InstanceIds=[instance_id])
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidInstanceID.NotFound":
                raise ProviderError(f"Instance not found: {vm_id}")
            raise ProviderError(f"AWS terminate error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

    def stop(self, vm_id: str) -> bool:
        """
        Stop an EC2 instance. The EBS root volume is preserved, matching
        native EC2 stop/start semantics.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_client()
        instance_id = vm_id.replace("aws-", "", 1)

        try:
            client.stop_instances(InstanceIds=[instance_id])
            return True
        except ClientError as e:
            raise ProviderError(f"AWS stop error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

    def start(self, vm_id: str) -> bool:
        """
        Start a previously stopped EC2 instance.

        Args:
            vm_id: VM identifier

        Returns:
            True if successful
        """
        client = self._get_client()
        instance_id = vm_id.replace("aws-", "", 1)

        try:
            client.start_instances(InstanceIds=[instance_id])
            return True
        except ClientError as e:
            raise ProviderError(f"AWS start error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

    def list_instances(self) -> List[VMInfo]:
        """
        List all EC2 instances MiniSky has launched (tagged ManagedBy=minisky).

        Returns:
            List of VMInfo dictionaries
        """
        client = self._get_client()

        try:
            response = client.describe_instances(
                Filters=[
                    {"Name": "tag:ManagedBy", "Values": ["minisky"]},
                    {"Name": "instance-state-name", "Values": list(_STATE_MAP.keys())},
                ]
            )
        except ClientError as e:
            raise ProviderError(f"AWS list error: {e.response.get('Error', {}).get('Message', str(e))}")
        except BotoCoreError as e:
            raise ProviderError(f"AWS connection error: {str(e)}")

        instances = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                state = instance.get("State", {}).get("Name", "unknown")
                name_tag = next(
                    (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                    "unnamed",
                )
                instances.append({
                    "vm_id": f"aws-{instance['InstanceId']}",
                    "ip_address": instance.get("PublicIpAddress", "pending"),
                    "ssh_port": 22,
                    "ssh_user": "ubuntu",
                    "status": _STATE_MAP.get(state, state),
                    "provider": "aws",
                    "task_name": name_tag,
                    "instance_id": instance["InstanceId"],
                    "instance_type": instance.get("InstanceType", "unknown"),
                })

        return instances

    def get_gpu_catalog(self) -> List[Dict[str, Any]]:
        """
        Get available GPU instance types with approximate on-demand pricing.

        Note: prices are static estimates (us-east-1 on-demand), not a
        live quote - AWS doesn't offer a simple public pricing endpoint
        the way RunPod/Lambda do.

        Returns:
            List of GPU catalog entries
        """
        catalog = []
        for (gpu_name, gpu_count), instance_type in _GPU_INSTANCE_MAP.items():
            catalog.append({
                "provider": "aws",
                "gpu_name": gpu_name,
                "instance_type": instance_type,
                "gpu_count": gpu_count,
                "price_per_hour": _APPROX_PRICE_PER_HOUR.get(instance_type),
                "price_is_estimate": True,
                "available": True,
            })
        return catalog
