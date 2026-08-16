"""Tests for the credential manager (minisky/credentials.py)."""

import os
import pytest
from unittest.mock import patch, MagicMock

from minisky.credentials import CredentialManager
from minisky.config import MiniSkyConfig


@pytest.fixture
def config(tmp_path):
    """Create a MiniSkyConfig in a temp directory."""
    return MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))


@pytest.fixture
def creds(config):
    """Create a CredentialManager with temp config."""
    return CredentialManager(config=config)


class TestGetApiKey:
    def test_from_env_variable(self, creds):
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "env-key-12345"}):
            key = creds.get_api_key("runpod")
            assert key == "env-key-12345"

    def test_from_config(self, creds, config):
        config.set("providers.runpod.api_key", "config-key-99")
        key = creds.get_api_key("runpod")
        assert key == "config-key-99"

    def test_env_takes_priority_over_config(self, creds, config):
        config.set("providers.runpod.api_key", "config-key")
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "env-key"}):
            key = creds.get_api_key("runpod")
            assert key == "env-key"

    def test_not_found_returns_none(self, creds):
        with patch.dict(os.environ, {}, clear=True):
            key = creds.get_api_key("runpod")
            assert key is None

    def test_lambda_key_from_env(self, creds):
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "lam-key"}):
            key = creds.get_api_key("lambda")
            assert key == "lam-key"

    def test_case_insensitive(self, creds, config):
        config.set("providers.runpod.api_key", "my-key")
        key = creds.get_api_key("RUNPOD")
        assert key == "my-key"

    def test_unknown_provider_no_env_var(self, creds):
        # Providers not in _ENV_VARS only check config
        key = creds.get_api_key("unknown_provider")
        assert key is None


class TestSetApiKey:
    def test_set_and_retrieve(self, creds, config):
        creds.set_api_key("runpod", "new-key-123")
        assert config.get("providers.runpod.api_key") == "new-key-123"


class TestIsConfigured:
    def test_is_configured_true(self, creds, config):
        config.set("providers.runpod.api_key", "some-key")
        assert creds.is_configured("runpod") is True

    def test_is_configured_false(self, creds):
        assert creds.is_configured("runpod") is False

    def test_is_configured_from_env(self, creds):
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "lam-key"}):
            assert creds.is_configured("lambda") is True


class TestIsAwsConfigured:
    def test_aws_from_config(self, creds, config):
        config.set("providers.aws.access_key_id", "AKIA123")
        config.set("providers.aws.secret_access_key", "secret123")
        assert creds.is_aws_configured() is True

    def test_aws_partial_config(self, creds, config):
        # Only access_key but no secret
        config.set("providers.aws.access_key_id", "AKIA123")
        # Should fall through to boto3
        with patch("minisky.credentials.boto3") as mock_boto3:
            mock_session = MagicMock()
            mock_session.get_credentials.return_value = None
            mock_boto3.Session.return_value = mock_session
            assert creds.is_aws_configured() is False

    def test_aws_from_boto3_session(self, creds):
        with patch("minisky.credentials.boto3") as mock_boto3:
            mock_session = MagicMock()
            mock_session.get_credentials.return_value = MagicMock()
            mock_boto3.Session.return_value = mock_session
            assert creds.is_aws_configured() is True

    def test_aws_boto3_import_error(self, creds):
        with patch("minisky.credentials.boto3") as mock_boto3:
            mock_boto3.Session.side_effect = Exception("no boto3")
            assert creds.is_aws_configured() is False


class TestIsGcpConfigured:
    def test_gcp_no_project(self, creds):
        assert creds.is_gcp_configured() is False

    def test_gcp_with_project_and_creds_path(self, creds, config):
        config.set("providers.gcp.project", "my-project")
        config.set("providers.gcp.credentials_path", "/path/to/creds.json")
        assert creds.is_gcp_configured() is True

    def test_gcp_with_project_and_google_auth(self, creds, config):
        config.set("providers.gcp.project", "my-project")
        with patch("minisky.credentials.google.auth.default") as mock_auth:
            mock_auth.return_value = (MagicMock(), "project")
            assert creds.is_gcp_configured() is True

    def test_gcp_with_project_but_no_auth(self, creds, config):
        config.set("providers.gcp.project", "my-project")
        with patch("minisky.credentials.google.auth.default") as mock_auth:
            mock_auth.side_effect = Exception("no credentials")
            assert creds.is_gcp_configured() is False


class TestGetConfiguredProviders:
    def test_mock_always_included(self, creds):
        providers = creds.get_configured_providers()
        assert "mock" in providers

    def test_includes_configured_providers(self, creds, config):
        config.set("providers.runpod.api_key", "rp-key")
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "lam-key"}):
            providers = creds.get_configured_providers()
            assert "mock" in providers
            assert "runpod" in providers
            assert "lambda" in providers


class TestRequireApiKey:
    def test_returns_key_when_present(self, creds, config):
        config.set("providers.runpod.api_key", "my-key")
        assert creds.require_api_key("runpod") == "my-key"

    def test_raises_when_missing(self, creds):
        with pytest.raises(ValueError, match="No API key found"):
            creds.require_api_key("runpod")

    def test_error_message_includes_provider_name(self, creds):
        with pytest.raises(ValueError, match="runpod"):
            creds.require_api_key("runpod")

    def test_error_message_includes_env_var_hint(self, creds):
        with pytest.raises(ValueError, match="RUNPOD_API_KEY"):
            creds.require_api_key("runpod")


class TestCredentialManagerInit:
    def test_default_init(self):
        cm = CredentialManager()
        assert cm._config is not None
