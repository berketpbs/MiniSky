"""Tests for the log manager (minisky/logger.py)."""

import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from minisky.logger import LogManager
from minisky.config import MiniSkyConfig


@pytest.fixture
def config(tmp_path):
    """Create a MiniSkyConfig pointing to a temp directory."""
    return MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))


@pytest.fixture
def log_manager(config):
    """Create a LogManager with temp-backed config."""
    return LogManager(config=config)


class TestGetLogPath:
    def test_returns_path_with_vm_id(self, log_manager):
        path = log_manager.get_log_path("mock-abc123")
        assert path.name == "mock-abc123.log"
        assert path.parent == log_manager._log_dir

    def test_different_vms_get_different_paths(self, log_manager):
        path1 = log_manager.get_log_path("vm-1")
        path2 = log_manager.get_log_path("vm-2")
        assert path1 != path2


class TestWriteLog:
    def test_write_creates_file(self, log_manager):
        log_manager.write_log("vm-1", "Hello world")
        path = log_manager.get_log_path("vm-1")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Hello world" in content

    def test_write_includes_timestamp(self, log_manager):
        log_manager.write_log("vm-1", "test line")
        content = log_manager.get_log_path("vm-1").read_text(encoding="utf-8")
        # Timestamp format: [YYYY-MM-DD HH:MM:SS]
        assert content.startswith("[")
        assert "]" in content

    def test_write_appends(self, log_manager):
        log_manager.write_log("vm-1", "line 1")
        log_manager.write_log("vm-1", "line 2")
        content = log_manager.get_log_path("vm-1").read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert "line 1" in lines[0]
        assert "line 2" in lines[1]

    def test_write_multiline(self, log_manager):
        log_manager.write_log("vm-1", "first\nsecond\nthird")
        content = log_manager.get_log_path("vm-1").read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 3


class TestReadLogs:
    def test_read_all(self, log_manager):
        log_manager.write_log("vm-1", "alpha")
        log_manager.write_log("vm-1", "beta")
        log_manager.write_log("vm-1", "gamma")
        result = log_manager.read_logs("vm-1")
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result

    def test_read_tail(self, log_manager):
        for i in range(10):
            log_manager.write_log("vm-1", f"line {i}")
        result = log_manager.read_logs("vm-1", tail=3)
        lines = result.strip().split("\n")
        assert len(lines) == 3
        assert "line 7" in lines[0]
        assert "line 8" in lines[1]
        assert "line 9" in lines[2]

    def test_read_no_file(self, log_manager):
        result = log_manager.read_logs("nonexistent-vm")
        assert result == ""

    def test_read_tail_zero_returns_all(self, log_manager):
        log_manager.write_log("vm-1", "a")
        log_manager.write_log("vm-1", "b")
        result = log_manager.read_logs("vm-1", tail=0)
        assert "a" in result
        assert "b" in result


class TestStreamLogs:
    @patch("minisky.logger.paramiko.SSHClient")
    def test_stream_logs_non_follow(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client

        # Simulate stdout with a few lines
        mock_stdout = iter(["line 1\n", "line 2\n"])
        mock_stderr = iter([])
        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        vm_info = {
            "vm_id": "test-vm",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
        }

        log_manager.stream_logs(vm_info, follow=False, tail=10)

        mock_client.connect.assert_called_once()
        mock_client.exec_command.assert_called_once()
        cmd_arg = mock_client.exec_command.call_args[0][0]
        assert "tail -n 10" in cmd_arg
        assert "-f" not in cmd_arg
        mock_client.close.assert_called_once()

    @patch("minisky.logger.paramiko.SSHClient")
    def test_stream_logs_follow_mode(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.exec_command.return_value = (MagicMock(), iter([]), iter([]))

        vm_info = {"vm_id": "vm-1", "ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        log_manager.stream_logs(vm_info, follow=True, tail=20)

        cmd_arg = mock_client.exec_command.call_args[0][0]
        assert "-f" in cmd_arg
        assert "tail -n 20" in cmd_arg

    @patch("minisky.logger.paramiko.SSHClient")
    def test_stream_logs_with_key_path(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.exec_command.return_value = (MagicMock(), iter([]), iter([]))

        with patch("minisky.logger.paramiko.RSAKey.from_private_key_file") as mock_key:
            mock_key.return_value = MagicMock()
            vm_info = {
                "vm_id": "vm-1", "ip_address": "10.0.0.1",
                "ssh_port": 22, "ssh_user": "ubuntu",
                "ssh_key_path": "/tmp/fake_key",
            }
            log_manager.stream_logs(vm_info, follow=False)
            mock_key.assert_called_once_with("/tmp/fake_key")

    @patch("minisky.logger.paramiko.SSHClient")
    def test_stream_logs_connection_error(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.connect.side_effect = Exception("Connection refused")

        vm_info = {"vm_id": "vm-1", "ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        # Should not raise, just log the error
        log_manager.stream_logs(vm_info, follow=False)
        mock_client.close.assert_called_once()

    @patch("minisky.logger.paramiko.SSHClient")
    def test_stream_logs_with_callback(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.exec_command.return_value = (MagicMock(), iter(["data\n"]), iter([]))

        captured = []
        vm_info = {"vm_id": "vm-1", "ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        log_manager.stream_logs(vm_info, on_line=lambda line: captured.append(line))
        assert "data" in captured


class TestStreamLogsBackground:
    @patch("minisky.logger.paramiko.SSHClient")
    def test_starts_daemon_thread(self, mock_ssh_cls, log_manager):
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.exec_command.return_value = (MagicMock(), iter([]), iter([]))

        vm_info = {"vm_id": "vm-bg", "ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        thread = log_manager.stream_logs_background(vm_info)

        assert thread.daemon is True
        assert thread.name == "log-vm-bg"
        assert "vm-bg" in log_manager._active_streams
        thread.join(timeout=2)


class TestLogManagerInit:
    def test_default_init(self):
        manager = LogManager()
        assert manager._log_dir is not None

    def test_init_with_config(self, config):
        manager = LogManager(config=config)
        assert manager._log_dir == config.log_dir
