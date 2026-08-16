"""Tests for the RunPod provider (mocked httpx, no real API calls)."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import httpx

from minisky.providers.runpod import RunPodProvider, _GPU_TYPE_MAP
from minisky.providers.base import ProviderError
from minisky.task import Task, ResourceRequirements


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """Create a RunPodProvider with mocked credentials."""
    with patch("minisky.providers.runpod.CredentialManager") as mock_creds_cls:
        mock_creds = MagicMock()
        mock_creds.require_api_key.return_value = "rp_test_key_12345"
        mock_creds_cls.return_value = mock_creds
        p = RunPodProvider()
        # Pre-create a mock client so tests can set up return values
        p._client = MagicMock(spec=httpx.Client)
        p._api_key = "rp_test_key_12345"
        yield p


@pytest.fixture
def sample_task():
    return Task(
        name="test-gpu-job",
        provider="runpod",
        resources=ResourceRequirements(gpu="A100", gpu_count=1, disk_gb=50),
        run=["python train.py"],
    )


@pytest.fixture
def sample_task_no_gpu():
    return Task(
        name="test-cpu-job",
        provider="runpod",
        resources=ResourceRequirements(gpu=None),
        run=["echo hello"],
    )


# ---------------------------------------------------------------------------
# GPU type resolution
# ---------------------------------------------------------------------------

class TestResolveGpuType:
    def test_known_gpu(self, provider):
        assert provider._resolve_gpu_type("A100") == "NVIDIA A100 80GB PCIe"

    def test_known_gpu_case_insensitive(self, provider):
        assert provider._resolve_gpu_type("rtx4090") == "NVIDIA GeForce RTX 4090"

    def test_unknown_gpu_passthrough(self, provider):
        assert provider._resolve_gpu_type("custom-gpu-xyz") == "custom-gpu-xyz"

    def test_none_gpu(self, provider):
        assert provider._resolve_gpu_type(None) is None


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

class TestRunPodLaunch:
    def test_launch_success(self, provider, sample_task):
        # POST /pods returns pod info
        provider._client.post.return_value = _mock_response(200, {
            "pod": {"id": "abc123", "sshPort": 22}
        })
        # GET /pods/{id} for _wait_for_ip returns IP
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"publicIp": "203.0.113.50", "desiredStatus": "RUNNING"}
        })

        vm_info = provider.launch(sample_task)

        assert vm_info["vm_id"] == "runpod-abc123"
        assert vm_info["ip_address"] == "203.0.113.50"
        assert vm_info["provider"] == "runpod"
        assert vm_info["status"] == "running"
        assert vm_info["ssh_user"] == "root"
        assert vm_info["task_name"] == "test-gpu-job"
        provider._client.post.assert_called_once()

    def test_launch_no_gpu_raises(self, provider, sample_task_no_gpu):
        with pytest.raises(ProviderError, match="GPU type is required"):
            provider.launch(sample_task_no_gpu)

    def test_launch_api_error(self, provider, sample_task):
        provider._client.post.return_value = _mock_response(
            500, text="Internal Server Error"
        )
        with pytest.raises(ProviderError, match="RunPod API error"):
            provider.launch(sample_task)

    def test_launch_connection_error(self, provider, sample_task):
        provider._client.post.side_effect = httpx.RequestError(
            "Connection refused", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="RunPod connection error"):
            provider.launch(sample_task)

    def test_launch_with_spot(self, provider):
        task = Task(
            name="spot-job",
            provider="runpod",
            resources=ResourceRequirements(gpu="A100", use_spot=True),
            run=["echo hi"],
        )
        provider._client.post.return_value = _mock_response(200, {
            "pod": {"id": "spot1"}
        })
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"publicIp": "10.0.0.1"}
        })

        vm_info = provider.launch(task)
        call_args = provider._client.post.call_args
        payload = call_args[1]["json"]
        assert payload["interruptible"] is True
        assert vm_info["spot"] is True


# ---------------------------------------------------------------------------
# _wait_for_ip
# ---------------------------------------------------------------------------

class TestWaitForIp:
    def test_wait_for_ip_immediate(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"publicIp": "1.2.3.4"}
        })
        ip = provider._wait_for_ip("pod1", timeout=5)
        assert ip == "1.2.3.4"

    def test_wait_for_ip_error_state(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"desiredStatus": "TERMINATED"}
        })
        with pytest.raises(ProviderError, match="TERMINATED"):
            provider._wait_for_ip("pod1", timeout=5)

    @patch("minisky.providers.runpod.time.sleep", return_value=None)
    @patch("minisky.providers.runpod.time.time")
    def test_wait_for_ip_timeout(self, mock_time, mock_sleep, provider):
        # Simulate time passing beyond timeout
        mock_time.side_effect = [0, 0, 200]  # start, check, expired
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"desiredStatus": "CREATED"}
        })
        with pytest.raises(ProviderError, match="Timeout"):
            provider._wait_for_ip("pod1", timeout=10)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestRunPodStatus:
    def test_status_success(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "pod": {
                "publicIp": "203.0.113.50",
                "sshPort": 22,
                "desiredStatus": "RUNNING",
                "name": "my-pod",
            }
        })
        status = provider.status("runpod-abc123")
        assert status["vm_id"] == "runpod-abc123"
        assert status["status"] == "running"
        assert status["ip_address"] == "203.0.113.50"

    def test_status_not_found(self, provider):
        provider._client.get.return_value = _mock_response(404, text="Not found")
        with pytest.raises(ProviderError, match="Pod not found"):
            provider.status("runpod-nonexistent")

    def test_status_maps_exited_to_stopped(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "pod": {"desiredStatus": "EXITED", "name": "p"}
        })
        status = provider.status("runpod-x")
        assert status["status"] == "stopped"


# ---------------------------------------------------------------------------
# Terminate
# ---------------------------------------------------------------------------

class TestRunPodTerminate:
    def test_terminate_success(self, provider):
        provider._client.delete.return_value = _mock_response(200)
        assert provider.terminate("runpod-abc123") is True

    def test_terminate_not_found(self, provider):
        provider._client.delete.return_value = _mock_response(404, text="Not found")
        with pytest.raises(ProviderError, match="Pod not found"):
            provider.terminate("runpod-nonexistent")

    def test_terminate_api_error(self, provider):
        provider._client.delete.return_value = _mock_response(500, text="Server error")
        with pytest.raises(ProviderError, match="RunPod API error"):
            provider.terminate("runpod-x")


# ---------------------------------------------------------------------------
# Stop / Start
# ---------------------------------------------------------------------------

class TestRunPodStopStart:
    def test_stop_success(self, provider):
        provider._client.post.return_value = _mock_response(200)
        assert provider.stop("runpod-abc123") is True
        provider._client.post.assert_called_with("/pods/abc123/stop")

    def test_stop_api_error(self, provider):
        provider._client.post.return_value = _mock_response(500, text="error")
        with pytest.raises(ProviderError, match="RunPod stop error"):
            provider.stop("runpod-x")

    def test_start_success(self, provider):
        provider._client.post.return_value = _mock_response(200)
        assert provider.start("runpod-abc123") is True
        provider._client.post.assert_called_with("/pods/abc123/start")

    def test_start_api_error(self, provider):
        provider._client.post.return_value = _mock_response(500, text="error")
        with pytest.raises(ProviderError, match="RunPod start error"):
            provider.start("runpod-x")


# ---------------------------------------------------------------------------
# List instances
# ---------------------------------------------------------------------------

class TestRunPodListInstances:
    def test_list_instances_empty(self, provider):
        provider._client.get.return_value = _mock_response(200, {"pods": []})
        assert provider.list_instances() == []

    def test_list_instances_with_pods(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "pods": [
                {"id": "p1", "publicIp": "1.2.3.4", "desiredStatus": "RUNNING", "name": "pod1", "sshPort": 22},
                {"id": "p2", "publicIp": "5.6.7.8", "desiredStatus": "EXITED", "name": "pod2", "sshPort": 22},
            ]
        })
        instances = provider.list_instances()
        assert len(instances) == 2
        assert instances[0]["vm_id"] == "runpod-p1"
        assert instances[1]["vm_id"] == "runpod-p2"

    def test_list_instances_connection_error(self, provider):
        provider._client.get.side_effect = httpx.RequestError(
            "timeout", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="RunPod connection error"):
            provider.list_instances()


# ---------------------------------------------------------------------------
# GPU catalog
# ---------------------------------------------------------------------------

class TestRunPodGpuCatalog:
    def test_get_gpu_catalog(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "gpuTypes": [
                {
                    "displayName": "NVIDIA A100 80GB",
                    "id": "NVIDIA A100 80GB PCIe",
                    "memoryInGb": 80,
                    "available": True,
                    "securePrice": 1.99,
                    "communitySpotPrice": 0.89,
                },
            ]
        })
        catalog = provider.get_gpu_catalog()
        assert len(catalog) == 1
        assert catalog[0]["gpu_name"] == "NVIDIA A100 80GB"
        assert catalog[0]["price_per_hour"] == 1.99
        assert catalog[0]["spot_price"] == 0.89
        assert catalog[0]["provider"] == "runpod"

    def test_get_gpu_catalog_connection_error(self, provider):
        provider._client.get.side_effect = httpx.RequestError(
            "timeout", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="RunPod catalog error"):
            provider.get_gpu_catalog()
