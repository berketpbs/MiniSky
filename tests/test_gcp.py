"""Tests for the GCP Compute Engine provider (mocked compute_v1, no real GCP calls)."""

import pytest
from unittest.mock import MagicMock, patch
from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import compute_v1

from minisky.providers.gcp import GCPProvider
from minisky.providers.base import ProviderError
from minisky.task import Task, ResourceRequirements


def _instance(name="minisky-abc123ef", status="RUNNING", ip="203.0.113.10", machine_type="n1-standard-4"):
    return compute_v1.Instance(
        name=name,
        status=status,
        machine_type=f"zones/us-central1-a/machineTypes/{machine_type}",
        network_interfaces=[compute_v1.NetworkInterface(
            access_configs=[compute_v1.AccessConfig(nat_i_p=ip)] if ip else [],
        )],
    )


def _success_operation():
    op = MagicMock()
    op.error_code = None
    op.result.return_value = None
    return op


@pytest.fixture
def sample_task():
    return Task(
        name="test-task",
        provider="gcp",
        resources=ResourceRequirements(gpu="T4", gpu_count=1),
        run=["echo hello"],
    )


@pytest.fixture
def provider(tmp_path):
    with patch("minisky.providers.gcp.MiniSkyConfig") as mock_config_cls:
        mock_config = MagicMock()
        # project configured, everything else default
        mock_config.get.side_effect = lambda key, default=None: (
            "my-project" if key == "providers.gcp.project" else default
        )
        mock_config_cls.return_value = mock_config
        yield GCPProvider()


class TestGCPProviderLaunch:
    def test_launch_success(self, provider, sample_task):
        mock_client = MagicMock()
        mock_client.insert.return_value = _success_operation()
        mock_client.get.return_value = _instance()

        fake_uuid = MagicMock()
        fake_uuid.hex = "abc123ef"

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client), \
             patch("minisky.providers.gcp.uuid.uuid4", return_value=fake_uuid):
            vm_info = provider.launch(sample_task)

        assert vm_info["vm_id"] == "gcp-minisky-abc123ef"
        assert vm_info["ip_address"] == "203.0.113.10"
        assert vm_info["provider"] == "gcp"
        assert vm_info["machine_type"] == "n1-standard-4"
        assert vm_info["ssh_user"] == "ubuntu"

        insert_kwargs = mock_client.insert.call_args.kwargs
        assert insert_kwargs["project"] == "my-project"
        instance_resource = insert_kwargs["instance_resource"]
        assert instance_resource.labels["managed-by"] == "minisky"
        assert instance_resource.guest_accelerators[0].accelerator_count == 1

    def test_launch_missing_gpu(self, provider):
        task = Task(name="t", provider="gcp", run=["echo hi"])
        with pytest.raises(ProviderError, match="GPU type is required"):
            provider.launch(task)

    def test_launch_unsupported_gpu_combo(self, provider):
        task = Task(
            name="t",
            provider="gcp",
            resources=ResourceRequirements(gpu="A100", gpu_count=3),
            run=["echo hi"],
        )
        with pytest.raises(ProviderError, match="No GCP machine config"):
            provider.launch(task)

    def test_launch_missing_project_raises_clear_error(self, sample_task):
        with patch("minisky.providers.gcp.MiniSkyConfig") as mock_config_cls:
            mock_config = MagicMock()
            mock_config.get.return_value = None
            mock_config_cls.return_value = mock_config
            provider = GCPProvider()

        with pytest.raises(ProviderError, match="GCP project is required"):
            provider.launch(sample_task)

    def test_launch_error_wrapped(self, provider, sample_task):
        mock_client = MagicMock()
        mock_client.insert.side_effect = GoogleAPIError("quota exceeded")

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            with pytest.raises(ProviderError, match="GCP launch error"):
                provider.launch(sample_task)

    def test_a100_uses_bundled_machine_type_no_separate_accelerator(self, provider):
        task = Task(
            name="t",
            provider="gcp",
            resources=ResourceRequirements(gpu="A100", gpu_count=1),
            run=["echo hi"],
        )
        mock_client = MagicMock()
        mock_client.insert.return_value = _success_operation()
        mock_client.get.return_value = _instance(machine_type="a2-highgpu-1g")

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            vm_info = provider.launch(task)

        assert vm_info["machine_type"] == "a2-highgpu-1g"
        instance_resource = mock_client.insert.call_args.kwargs["instance_resource"]
        assert len(instance_resource.guest_accelerators) == 0


class TestGCPProviderLifecycle:
    def test_status(self, provider):
        mock_client = MagicMock()
        mock_client.get.return_value = _instance(status="TERMINATED", ip=None)

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            info = provider.status("gcp-minisky-abc123")

        assert info["status"] == "stopped"  # GCE's TERMINATED means stopped, not deleted
        assert info["ip_address"] == "pending"

    def test_status_not_found(self, provider):
        mock_client = MagicMock()
        mock_client.get.side_effect = NotFound("no such instance")

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            with pytest.raises(ProviderError, match="Instance not found"):
                provider.status("gcp-missing")

    def test_terminate(self, provider):
        mock_client = MagicMock()
        mock_client.delete.return_value = _success_operation()

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            assert provider.terminate("gcp-minisky-abc123") is True
        mock_client.delete.assert_called_once_with(
            project="my-project", zone="us-central1-a", instance="minisky-abc123"
        )

    def test_stop_and_start(self, provider):
        mock_client = MagicMock()
        mock_client.stop.return_value = _success_operation()
        mock_client.start.return_value = _success_operation()

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            assert provider.stop("gcp-minisky-abc123") is True
            assert provider.start("gcp-minisky-abc123") is True

    def test_list_instances_filters_by_label(self, provider):
        mock_client = MagicMock()
        mock_client.list.return_value = [_instance()]

        with patch("minisky.providers.gcp.compute_v1.InstancesClient", return_value=mock_client):
            instances = provider.list_instances()

        assert len(instances) == 1
        assert instances[0]["vm_id"] == "gcp-minisky-abc123ef"
        list_kwargs = mock_client.list.call_args.kwargs
        assert "managed-by = minisky" in list_kwargs["filter"]


class TestGCPProviderCatalog:
    def test_get_gpu_catalog_no_network_call(self, provider):
        with patch("minisky.providers.gcp.compute_v1.InstancesClient") as mock_cls:
            catalog = provider.get_gpu_catalog()
            mock_cls.assert_not_called()

        assert len(catalog) > 0
        assert all(entry["provider"] == "gcp" for entry in catalog)
        assert all(entry["price_is_estimate"] is True for entry in catalog)
