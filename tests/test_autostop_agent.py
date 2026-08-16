"""Tests for the autostop agent's SSH resource monitor (minisky/autostop_agent.py)."""

from unittest.mock import MagicMock, patch

from minisky.autostop_agent import ResourceMonitor, AutostopAgent, AutostopConfig
from minisky.state import StateManager


class TestResourceMonitorConnect:
    def test_uses_key_filename_when_configured(self):
        vm_info = {
            "ip_address": "203.0.113.5",
            "ssh_port": 22,
            "ssh_user": "root",
            "ssh_key_path": "/home/user/.ssh/custom_key",
        }
        monitor = ResourceMonitor(vm_info)

        mock_client = MagicMock()
        with patch("minisky.autostop_agent.paramiko.SSHClient", return_value=mock_client):
            result = monitor._connect()

        assert result is True
        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["key_filename"] == "/home/user/.ssh/custom_key"
        assert "look_for_keys" not in kwargs

    def test_falls_back_to_look_for_keys_without_key_path(self):
        vm_info = {"ip_address": "203.0.113.5", "ssh_port": 22, "ssh_user": "root"}
        monitor = ResourceMonitor(vm_info)

        mock_client = MagicMock()
        with patch("minisky.autostop_agent.paramiko.SSHClient", return_value=mock_client):
            monitor._connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["look_for_keys"] is True
        assert "key_filename" not in kwargs


class TestAutostopAgentStop:
    def test_stop_clears_thread_when_it_exits_in_time(self, tmp_path):
        agent = AutostopAgent(
            state=StateManager(db_path=str(tmp_path / "state.db")),
            autostop_config=AutostopConfig(check_interval_seconds=1),
        )
        agent.start()
        agent.stop()
        assert agent._thread is None

    def test_stop_keeps_thread_reference_if_still_alive(self):
        agent = AutostopAgent()
        agent._running = True
        stuck_thread = MagicMock()
        stuck_thread.is_alive.return_value = True
        agent._thread = stuck_thread

        agent.stop()

        stuck_thread.join.assert_called_once_with(timeout=5)
        assert agent._thread is stuck_thread
        assert agent._running is False
