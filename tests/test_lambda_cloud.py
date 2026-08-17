"""Tests for the Lambda Cloud provider (mocked httpx, no real API calls)."""

import pytest
from unittest.mock import MagicMock, patch
import httpx

from minisky.providers.lambda_cloud import LambdaProvider
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


INSTANCE_TYPES_RESPONSE = {
    "data": {
        "gpu_1x_a100_sxm4": {
            "instance_type": {
                "name": "gpu_1x_a100_sxm4",
                "description": "1x NVIDIA A100 SXM4 (40 GB)",
                "price_cents_per_hour": 110,
                "specs": {"gpus": 1, "ram_gib": 64, "vcpus": 8, "storage_gib": 512},
            },
            "regions_with_capacity_available": [
                {"name": "us-east-1", "description": "US East"}
            ],
        },
        "gpu_8x_a100_80gb_sxm4": {
            "instance_type": {
                "name": "gpu_8x_a100_80gb_sxm4",
                "description": "8x NVIDIA A100 80GB SXM4",
                "price_cents_per_hour": 1200,
                "specs": {"gpus": 8, "ram_gib": 640, "vcpus": 120, "storage_gib": 4096},
            },
            "regions_with_capacity_available": [],
        },
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """Create a LambdaProvider with mocked credentials."""
    with patch("minisky.providers.lambda_cloud.CredentialManager") as mock_creds_cls:
        mock_creds = MagicMock()
        mock_creds.require_api_key.return_value = "lambda_test_key_12345"
        mock_creds_cls.return_value = mock_creds
        p = LambdaProvider()
        p._client = MagicMock(spec=httpx.Client)
        p._api_key = "lambda_test_key_12345"
        yield p


@pytest.fixture
def sample_task():
    return Task(
        name="test-lambda-job",
        provider="lambda",
        resources=ResourceRequirements(gpu="A100", gpu_count=1),
        run=["python train.py"],
    )


# ---------------------------------------------------------------------------
# _resolve_instance_type
# ---------------------------------------------------------------------------

class TestResolveInstanceType:
    def test_resolve_cheapest_available(self, provider, sample_task):
        provider._client.get.return_value = _mock_response(200, INSTANCE_TYPES_RESPONSE)
        type_name, region = provider._resolve_instance_type(sample_task)
        assert type_name == "gpu_1x_a100_sxm4"
        assert region == "us-east-1"

    def test_resolve_no_available_raises(self, provider):
        # Only the 8x type matches but has no capacity
        task = Task(
            name="t", provider="lambda",
            resources=ResourceRequirements(gpu="A100", gpu_count=8),
            run=["echo"],
        )
        response_data = {
            "data": {
                "gpu_8x_a100_80gb_sxm4": {
                    "instance_type": {
                        "name": "gpu_8x_a100_80gb_sxm4",
                        "description": "8x NVIDIA A100",
                        "price_cents_per_hour": 1200,
                        "specs": {"gpus": 8},
                    },
                    "regions_with_capacity_available": [],
                },
            }
        }
        provider._client.get.return_value = _mock_response(200, response_data)
        with pytest.raises(ProviderError, match="No available Lambda instance type"):
            provider._resolve_instance_type(task)

    def test_resolve_api_error(self, provider, sample_task):
        provider._client.get.side_effect = httpx.RequestError(
            "connection refused", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="Lambda API error"):
            provider._resolve_instance_type(sample_task)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

class TestLambdaLaunch:
    def test_launch_success(self, provider, sample_task):
        # _resolve_instance_type: GET /instance-types
        # _get_ssh_keys: GET /ssh-keys
        # POST /instance-operations/launch
        # _wait_for_ip: GET /instances/{id}
        call_count = 0

        def mock_get(url):
            nonlocal call_count
            call_count += 1
            if "instance-types" in url:
                return _mock_response(200, INSTANCE_TYPES_RESPONSE)
            elif "ssh-keys" in url:
                return _mock_response(200, {"data": [{"name": "my-key"}]})
            elif "instances/" in url:
                return _mock_response(200, {"data": {"ip": "203.0.113.99", "status": "active"}})
            return _mock_response(200, {})

        provider._client.get.side_effect = mock_get
        provider._client.post.return_value = _mock_response(200, {
            "data": {"instance_ids": ["inst-abc123"]}
        })

        vm_info = provider.launch(sample_task)
        assert vm_info["vm_id"] == "lambda-inst-abc123"
        assert vm_info["ip_address"] == "203.0.113.99"
        assert vm_info["provider"] == "lambda"
        assert vm_info["ssh_user"] == "ubuntu"

    def test_launch_no_ssh_keys(self, provider, sample_task):
        def mock_get(url):
            if "instance-types" in url:
                return _mock_response(200, INSTANCE_TYPES_RESPONSE)
            elif "ssh-keys" in url:
                return _mock_response(200, {"data": []})
            return _mock_response(200, {})

        provider._client.get.side_effect = mock_get
        with pytest.raises(ProviderError, match="No SSH keys found"):
            provider.launch(sample_task)

    def test_launch_api_error(self, provider, sample_task):
        def mock_get(url):
            if "instance-types" in url:
                return _mock_response(200, INSTANCE_TYPES_RESPONSE)
            elif "ssh-keys" in url:
                return _mock_response(200, {"data": [{"name": "k"}]})
            return _mock_response(200, {})

        provider._client.get.side_effect = mock_get
        resp = _mock_response(400, text="Bad request")
        resp.json.return_value = {"error": {"message": "Insufficient capacity"}}
        provider._client.post.return_value = resp
        with pytest.raises(ProviderError, match="Lambda launch error"):
            provider.launch(sample_task)

    def test_launch_no_instance_ids(self, provider, sample_task):
        def mock_get(url):
            if "instance-types" in url:
                return _mock_response(200, INSTANCE_TYPES_RESPONSE)
            elif "ssh-keys" in url:
                return _mock_response(200, {"data": [{"name": "k"}]})
            return _mock_response(200, {})

        provider._client.get.side_effect = mock_get
        provider._client.post.return_value = _mock_response(200, {"data": {"instance_ids": []}})
        with pytest.raises(ProviderError, match="no instance IDs"):
            provider.launch(sample_task)


# ---------------------------------------------------------------------------
# _wait_for_ip
# ---------------------------------------------------------------------------

class TestLambdaWaitForIp:
    def test_wait_for_ip_immediate(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "data": {"ip": "10.0.0.1", "status": "active"}
        })
        ip = provider._wait_for_ip("inst1", timeout=5)
        assert ip == "10.0.0.1"

    def test_wait_for_ip_terminated_state(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "data": {"status": "terminated"}
        })
        with pytest.raises(ProviderError, match="terminated"):
            provider._wait_for_ip("inst1", timeout=5)

    def test_wait_for_ip_timeout(self, provider):
        """Test timeout by using a very short timeout with no IP returned."""
        provider._client.get.return_value = _mock_response(200, {
            "data": {"status": "booting"}
        })
        # Use timeout=0 to immediately trigger timeout
        with pytest.raises(ProviderError, match="Timeout"):
            provider._wait_for_ip("inst1", timeout=0)


# ---------------------------------------------------------------------------
# _extract_error_message
# ---------------------------------------------------------------------------

class TestExtractErrorMessage:
    def test_extract_json_error(self):
        resp = MagicMock()
        resp.json.return_value = {"error": {"message": "Quota exceeded"}}
        error = httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        assert LambdaProvider._extract_error_message(error) == "Quota exceeded"

    def test_extract_text_fallback(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("not json")
        resp.text = "raw error text"
        error = httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        assert LambdaProvider._extract_error_message(error) == "raw error text"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestLambdaStatus:
    def test_status_success(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "data": {"ip": "10.0.0.1", "status": "active", "name": "my-inst"}
        })
        status = provider.status("lambda-inst123")
        assert status["vm_id"] == "lambda-inst123"
        assert status["status"] == "active"
        assert status["ip_address"] == "10.0.0.1"

    def test_status_not_found(self, provider):
        provider._client.get.return_value = _mock_response(200, {"data": {}})
        with pytest.raises(ProviderError, match="Instance not found"):
            provider.status("lambda-nonexistent")


# ---------------------------------------------------------------------------
# Terminate
# ---------------------------------------------------------------------------

class TestLambdaTerminate:
    def test_terminate_success(self, provider):
        provider._client.post.return_value = _mock_response(200)
        assert provider.terminate("lambda-inst123") is True
        call_args = provider._client.post.call_args
        assert call_args[1]["json"]["instance_ids"] == ["inst123"]

    def test_terminate_api_error(self, provider):
        resp = _mock_response(400, text="Bad request")
        resp.json.return_value = {"error": {"message": "Not found"}}
        provider._client.post.return_value = resp
        with pytest.raises(ProviderError, match="Lambda terminate error"):
            provider.terminate("lambda-inst123")

    def test_terminate_connection_error(self, provider):
        provider._client.post.side_effect = httpx.RequestError(
            "timeout", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="Lambda connection error"):
            provider.terminate("lambda-inst123")


# ---------------------------------------------------------------------------
# Stop / Start (unsupported)
# ---------------------------------------------------------------------------

class TestLambdaStopStart:
    def test_stop_raises(self, provider):
        with pytest.raises(ProviderError, match="does not support stopping"):
            provider.stop("lambda-inst123")

    def test_start_raises(self, provider):
        with pytest.raises(ProviderError, match="does not support starting"):
            provider.start("lambda-inst123")


# ---------------------------------------------------------------------------
# List instances
# ---------------------------------------------------------------------------

class TestLambdaListInstances:
    def test_list_instances_empty(self, provider):
        provider._client.get.return_value = _mock_response(200, {"data": []})
        assert provider.list_instances() == []

    def test_list_instances_with_data(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "data": [
                {"id": "i1", "ip": "1.2.3.4", "status": "active", "name": "inst1"},
                {"id": "i2", "ip": "5.6.7.8", "status": "terminated", "name": "inst2"},
            ]
        })
        instances = provider.list_instances()
        assert len(instances) == 2
        assert instances[0]["vm_id"] == "lambda-i1"
        assert instances[0]["ssh_user"] == "ubuntu"
        assert instances[1]["status"] == "terminated"

    def test_list_instances_connection_error(self, provider):
        provider._client.get.side_effect = httpx.RequestError(
            "timeout", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="Lambda connection error"):
            provider.list_instances()


# ---------------------------------------------------------------------------
# GPU catalog
# ---------------------------------------------------------------------------

class TestLambdaGpuCatalog:
    def test_get_gpu_catalog(self, provider):
        provider._client.get.return_value = _mock_response(200, INSTANCE_TYPES_RESPONSE)
        catalog = provider.get_gpu_catalog()
        assert len(catalog) == 2

        entry = catalog[0]
        assert entry["provider"] == "lambda"
        assert entry["price_per_hour"] == 1.10  # 110 cents / 100
        assert entry["gpu_count"] == 1
        assert entry["available"] is True

        unavail = catalog[1]
        assert unavail["available"] is False

    def test_get_gpu_catalog_connection_error(self, provider):
        provider._client.get.side_effect = httpx.RequestError(
            "timeout", request=MagicMock()
        )
        with pytest.raises(ProviderError, match="Lambda catalog error"):
            provider.get_gpu_catalog()


# ---------------------------------------------------------------------------
# _get_ssh_keys
# ---------------------------------------------------------------------------

class TestGetSshKeys:
    def test_returns_key_names(self, provider):
        provider._client.get.return_value = _mock_response(200, {
            "data": [{"name": "key1"}, {"name": "key2"}, {"name": None}]
        })
        keys = provider._get_ssh_keys()
        assert keys == ["key1", "key2"]

    def test_returns_empty_on_error(self, provider):
        provider._client.get.side_effect = httpx.RequestError(
            "error", request=MagicMock()
        )
        assert provider._get_ssh_keys() == []
