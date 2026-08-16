"""Tests for the AWS EC2 provider (mocked boto3, no real AWS calls)."""

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from minisky.providers.aws import AWSProvider
from minisky.providers.base import ProviderError
from minisky.task import Task, ResourceRequirements


def _client_error(code, message="error"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "TestOperation")


@pytest.fixture
def sample_task():
    return Task(
        name="test-task",
        provider="aws",
        resources=ResourceRequirements(gpu="T4", gpu_count=1, image_id="ami-fixed123"),
        run=["echo hello"],
    )


@pytest.fixture
def provider():
    with patch("minisky.providers.aws.MiniSkyConfig") as mock_config_cls:
        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_config_cls.return_value = mock_config
        yield AWSProvider()


class TestAWSProviderLaunch:
    def test_launch_success(self, provider, sample_task):
        mock_ec2 = MagicMock()
        mock_ec2.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-0123456789abcdef0"}]
        }
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-0123456789abcdef0",
                    "State": {"Name": "running"},
                    "PublicIpAddress": "203.0.113.10",
                }]
            }]
        }

        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            vm_info = provider.launch(sample_task)

        assert vm_info["vm_id"] == "aws-i-0123456789abcdef0"
        assert vm_info["ip_address"] == "203.0.113.10"
        assert vm_info["provider"] == "aws"
        assert vm_info["instance_type"] == "g4dn.xlarge"
        assert vm_info["ssh_user"] == "ubuntu"

        run_kwargs = mock_ec2.run_instances.call_args.kwargs
        assert run_kwargs["ImageId"] == "ami-fixed123"
        assert run_kwargs["InstanceType"] == "g4dn.xlarge"
        tags = run_kwargs["TagSpecifications"][0]["Tags"]
        assert {"Key": "ManagedBy", "Value": "minisky"} in tags

    def test_launch_missing_gpu(self, provider):
        task = Task(name="t", provider="aws", run=["echo hi"])
        with pytest.raises(ProviderError, match="GPU type is required"):
            provider.launch(task)

    def test_launch_unsupported_gpu_combo(self, provider):
        task = Task(
            name="t",
            provider="aws",
            resources=ResourceRequirements(gpu="A100", gpu_count=1, image_id="ami-x"),
            run=["echo hi"],
        )
        with pytest.raises(ProviderError, match="No AWS instance type"):
            provider.launch(task)

    def test_launch_client_error_wrapped(self, provider, sample_task):
        mock_ec2 = MagicMock()
        mock_ec2.run_instances.side_effect = _client_error("InsufficientInstanceCapacity")

        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            with pytest.raises(ProviderError, match="AWS launch error"):
                provider.launch(sample_task)

    def test_launch_resolves_ami_via_ssm_when_unset(self, provider):
        task = Task(
            name="t",
            provider="aws",
            resources=ResourceRequirements(gpu="T4", gpu_count=1),
            run=["echo hi"],
        )
        mock_ec2 = MagicMock()
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-abc"}]}
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-abc",
                "State": {"Name": "running"},
                "PublicIpAddress": "1.2.3.4",
            }]}]
        }
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-resolved"}}

        def client_factory(service, **kwargs):
            return mock_ec2 if service == "ec2" else mock_ssm

        with patch("minisky.providers.aws.boto3.client", side_effect=client_factory):
            vm_info = provider.launch(task)

        assert vm_info["vm_id"] == "aws-i-abc"
        assert mock_ec2.run_instances.call_args.kwargs["ImageId"] == "ami-resolved"


class TestAWSProviderLifecycle:
    def test_status(self, provider):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-abc",
                "State": {"Name": "stopped"},
                "InstanceType": "g4dn.xlarge",
                "Tags": [{"Key": "Name", "Value": "test-task"}],
            }]}]
        }
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            info = provider.status("aws-i-abc")

        assert info["status"] == "stopped"
        assert info["task_name"] == "test-task"

    def test_status_not_found(self, provider):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.side_effect = _client_error("InvalidInstanceID.NotFound")
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            with pytest.raises(ProviderError, match="Instance not found"):
                provider.status("aws-i-missing")

    def test_terminate(self, provider):
        mock_ec2 = MagicMock()
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            assert provider.terminate("aws-i-abc") is True
        mock_ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-abc"])

    def test_stop_and_start(self, provider):
        mock_ec2 = MagicMock()
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            assert provider.stop("aws-i-abc") is True
            assert provider.start("aws-i-abc") is True
        mock_ec2.stop_instances.assert_called_once_with(InstanceIds=["i-abc"])
        mock_ec2.start_instances.assert_called_once_with(InstanceIds=["i-abc"])

    def test_stop_network_error_wrapped(self, provider):
        from botocore.exceptions import EndpointConnectionError
        mock_ec2 = MagicMock()
        mock_ec2.stop_instances.side_effect = EndpointConnectionError(endpoint_url="https://ec2.amazonaws.com")
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            with pytest.raises(ProviderError, match="AWS connection error"):
                provider.stop("aws-i-abc")

    def test_list_instances_filters_by_tag(self, provider):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-abc",
                "State": {"Name": "running"},
                "InstanceType": "g4dn.xlarge",
                "PublicIpAddress": "1.2.3.4",
                "Tags": [{"Key": "Name", "Value": "job1"}],
            }]}]
        }
        with patch("minisky.providers.aws.boto3.client", return_value=mock_ec2):
            instances = provider.list_instances()

        assert len(instances) == 1
        assert instances[0]["vm_id"] == "aws-i-abc"
        filters = mock_ec2.describe_instances.call_args.kwargs["Filters"]
        assert {"Name": "tag:ManagedBy", "Values": ["minisky"]} in filters


class TestAWSProviderCatalog:
    def test_get_gpu_catalog_no_network_call(self, provider):
        with patch("minisky.providers.aws.boto3.client") as mock_client:
            catalog = provider.get_gpu_catalog()
            mock_client.assert_not_called()

        assert len(catalog) > 0
        assert all(entry["provider"] == "aws" for entry in catalog)
        assert all(entry["price_is_estimate"] is True for entry in catalog)
