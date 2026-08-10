"""
Tests for file_sync module.

Covers SyncConfig, SyncResult, exclude patterns, RsyncSyncer command building,
and SFTP fallback logic.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from minisky.file_sync import (
    SyncConfig,
    SyncResult,
    SyncDirection,
    RsyncSyncer,
    sync_workdir,
    download_results,
)


# ---------------------------------------------------------------------------
# SyncConfig tests
# ---------------------------------------------------------------------------

class TestSyncConfig:
    """Test sync configuration defaults and customization."""

    def test_default_exclude_patterns(self):
        config = SyncConfig()
        assert ".git" in config.exclude_patterns
        assert "__pycache__" in config.exclude_patterns
        assert "*.pyc" in config.exclude_patterns
        assert ".venv" in config.exclude_patterns
        assert "node_modules" in config.exclude_patterns

    def test_custom_config(self):
        config = SyncConfig(
            delete_extraneous=True,
            compress=False,
            dry_run=True,
            verbose=True,
        )
        assert config.delete_extraneous is True
        assert config.compress is False
        assert config.dry_run is True
        assert config.verbose is True

    def test_extend_exclude_patterns(self):
        config = SyncConfig()
        original_len = len(config.exclude_patterns)
        config.exclude_patterns.extend(["*.tmp", "data/"])
        assert len(config.exclude_patterns) == original_len + 2


# ---------------------------------------------------------------------------
# SyncResult tests
# ---------------------------------------------------------------------------

class TestSyncResult:
    """Test sync result data structure."""

    def test_success_result(self):
        result = SyncResult(
            success=True,
            files_transferred=42,
            bytes_transferred=1024 * 1024,
            duration_seconds=3.5,
            method="rsync",
        )
        assert result.success is True
        assert result.files_transferred == 42
        assert result.method == "rsync"

    def test_failure_result(self):
        result = SyncResult(
            success=False,
            error="Connection refused",
            method="sftp",
        )
        assert result.success is False
        assert result.error == "Connection refused"


# ---------------------------------------------------------------------------
# RsyncSyncer tests
# ---------------------------------------------------------------------------

class TestRsyncSyncer:
    """Test RsyncSyncer command building and logic."""

    @pytest.fixture
    def vm_info(self):
        return {
            "ip_address": "10.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "ssh_key_path": "/home/user/.ssh/id_rsa",
        }

    @pytest.fixture
    def syncer(self, vm_info):
        return RsyncSyncer(vm_info, SyncConfig())

    def test_rsync_command_upload(self, syncer):
        cmd = syncer._build_rsync_command(
            "/local/path", "/remote/path", SyncDirection.LOCAL_TO_REMOTE
        )
        assert "rsync" in cmd
        assert "-a" in cmd
        assert "-z" in cmd  # compress enabled by default
        assert "--progress" in cmd
        # Should have SSH options
        cmd_str = " ".join(cmd)
        assert "ssh -p 22" in cmd_str
        assert "root@10.0.0.1:/remote/path" in cmd_str

    def test_rsync_command_download(self, syncer):
        cmd = syncer._build_rsync_command(
            "/local/path", "/remote/path", SyncDirection.REMOTE_TO_LOCAL
        )
        cmd_str = " ".join(cmd)
        assert "root@10.0.0.1:/remote/path/" in cmd_str

    def test_rsync_command_with_delete(self, vm_info):
        config = SyncConfig(delete_extraneous=True)
        syncer = RsyncSyncer(vm_info, config)
        cmd = syncer._build_rsync_command(
            "/local", "/remote", SyncDirection.LOCAL_TO_REMOTE
        )
        assert "--delete" in cmd

    def test_rsync_command_dry_run(self, vm_info):
        config = SyncConfig(dry_run=True)
        syncer = RsyncSyncer(vm_info, config)
        cmd = syncer._build_rsync_command(
            "/local", "/remote", SyncDirection.LOCAL_TO_REMOTE
        )
        assert "--dry-run" in cmd

    def test_rsync_command_with_key(self, syncer):
        cmd = syncer._build_rsync_command(
            "/local", "/remote", SyncDirection.LOCAL_TO_REMOTE
        )
        cmd_str = " ".join(cmd)
        assert "-i /home/user/.ssh/id_rsa" in cmd_str

    def test_exclude_patterns_in_command(self, syncer):
        cmd = syncer._build_rsync_command(
            "/local", "/remote", SyncDirection.LOCAL_TO_REMOTE
        )
        assert "--exclude" in cmd
        # Check some default patterns are included
        exclude_pairs = list(zip(cmd, cmd[1:]))
        exclude_values = [v for k, v in exclude_pairs if k == "--exclude"]
        assert ".git" in exclude_values
        assert "__pycache__" in exclude_values


class TestExcludePatterns:
    """Test the _should_exclude method."""

    @pytest.fixture
    def syncer(self):
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        return RsyncSyncer(vm_info, SyncConfig())

    def test_exclude_git(self, syncer, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert syncer._should_exclude(git_dir, tmp_path) is True

    def test_exclude_pycache(self, syncer, tmp_path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        assert syncer._should_exclude(cache, tmp_path) is True

    def test_exclude_pyc_file(self, syncer, tmp_path):
        pyc = tmp_path / "module.pyc"
        pyc.touch()
        assert syncer._should_exclude(pyc, tmp_path) is True

    def test_include_python_file(self, syncer, tmp_path):
        py = tmp_path / "main.py"
        py.touch()
        assert syncer._should_exclude(py, tmp_path) is False

    def test_include_regular_dir(self, syncer, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        assert syncer._should_exclude(src, tmp_path) is False


class TestRsyncAvailability:
    """Test rsync availability detection."""

    def test_rsync_check_cached(self):
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        syncer = RsyncSyncer(vm_info)

        # Force a cached value
        syncer._rsync_available = True
        assert syncer._check_rsync_available() is True

        syncer._rsync_available = False
        assert syncer._check_rsync_available() is False

    @patch("subprocess.run")
    def test_rsync_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        syncer = RsyncSyncer(vm_info)
        syncer._rsync_available = None  # Reset cache
        assert syncer._check_rsync_available() is False


class TestSyncConvenienceFunctions:
    """Test sync_workdir and download_results helper functions."""

    @patch.object(RsyncSyncer, "sync")
    def test_sync_workdir(self, mock_sync, tmp_path):
        mock_sync.return_value = SyncResult(
            success=True, files_transferred=10, duration_seconds=1.5, method="rsync"
        )

        local_dir = tmp_path / "project"
        local_dir.mkdir()

        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        result = sync_workdir(vm_info, str(local_dir), "~/workdir")

        assert result.success is True
        mock_sync.assert_called_once()

    @patch.object(RsyncSyncer, "sync")
    def test_download_results(self, mock_sync, tmp_path):
        mock_sync.return_value = SyncResult(
            success=True, files_transferred=5, duration_seconds=2.0, method="sftp"
        )

        local_dir = tmp_path / "results"

        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        result = download_results(vm_info, "~/results", str(local_dir))

        assert result.success is True
        assert result.method == "sftp"
