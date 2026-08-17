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

    def test_not_found_returns_none(self, tmp_path):
        # Fresh config with no keys set, and ensure env var is not set
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty_config.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        with patch.dict(os.environ, {"RUNPOD_API_KEY": ""}, clear=False):
            os.environ.pop("RUNPOD_API_KEY", None)
            key = fresh_creds.get_api_key("runpod")
            assert key is None

    def test_lambda_key_from_env(self, creds):
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "lam-key"}):
            key = creds.get_api_key("lambda")
            assert key == "lam-key"

    def test_case_insensitive(self, creds, config):
        config.set("providers.runpod.api_key", "my-key")
        key = creds.get_api_key("RUNPOD")
        assert key == "my-key"

    def test_unknown_provider_no_env_var(self, tmp_path):
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty2.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        key = fresh_creds.get_api_key("unknown_provider")
        assert key is None


class TestSetApiKey:
    def test_set_and_retrieve(self, creds, config):
        creds.set_api_key("runpod", "new-key-123")
        assert config.get("providers.runpod.api_key") == "new-key-123"


class TestIsConfigured:
    def test_is_configured_true(self, creds, config):
        config.set("providers.runpod.api_key", "some-key")
        assert creds.is_configured("runpod") is True

    def test_is_configured_false(self, tmp_path):
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty3.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNPOD_API_KEY", None)
            assert fresh_creds.is_configured("runpod") is False

    def test_is_configured_from_env(self, creds):
        with patch.dict(os.environ, {"LAMBDA_API_KEY": "lam-key"}):
            assert creds.is_configured("lambda") is True


class TestIsAwsConfigured:
    def test_aws_from_config(self, creds, config):
        config.set("providers.aws.access_key_id", "AKIA123")
        config.set("providers.aws.secret_access_key", "secret123")
        assert creds.is_aws_configured() is True

    def test_aws_partial_config(self, creds, config):
        # Only access_key but no secret — should fall through to boto3
        config.set("providers.aws.access_key_id", "AKIA123")
        # boto3 is imported inside the method, mock it at the import level
        import minisky.credentials as creds_mod
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        mock_boto3 = MagicMock()
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None
        mock_boto3.Session.return_value = mock_session

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            assert creds.is_aws_configured() is False

    def test_aws_from_boto3_session(self, creds):
        mock_boto3 = MagicMock()
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = MagicMock()
        mock_boto3.Session.return_value = mock_session

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            assert creds.is_aws_configured() is True

    def test_aws_boto3_exception(self, creds):
        # When boto3.Session() raises, should return False
        mock_boto3 = MagicMock()
        mock_boto3.Session.side_effect = Exception("no boto3")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
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
        # google.auth is imported inside the method, mock at sys.modules level
        mock_google = MagicMock()
        mock_google.auth.default.return_value = (MagicMock(), "project")

        with patch.dict("sys.modules", {"google": mock_google, "google.auth": mock_google.auth}):
            assert creds.is_gcp_configured() is True

    def test_gcp_with_project_but_no_auth(self, creds, config):
        config.set("providers.gcp.project", "my-project")
        mock_google = MagicMock()
        mock_google.auth.default.side_effect = Exception("no credentials")

        with patch.dict("sys.modules", {"google": mock_google, "google.auth": mock_google.auth}):
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

    def test_raises_when_missing(self, tmp_path):
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty4.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNPOD_API_KEY", None)
            with pytest.raises(ValueError, match="No API key found"):
                fresh_creds.require_api_key("runpod")

    def test_error_message_includes_provider_name(self, tmp_path):
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty5.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNPOD_API_KEY", None)
            with pytest.raises(ValueError, match="runpod"):
                fresh_creds.require_api_key("runpod")

    def test_error_message_includes_env_var_hint(self, tmp_path):
        fresh_config = MiniSkyConfig(config_path=str(tmp_path / "empty6.yaml"))
        fresh_creds = CredentialManager(config=fresh_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNPOD_API_KEY", None)
            with pytest.raises(ValueError, match="RUNPOD_API_KEY"):
                fresh_creds.require_api_key("runpod")


class TestCredentialManagerInit:
    def test_default_init(self):
        cm = CredentialManager()
        assert cm._config is not None
