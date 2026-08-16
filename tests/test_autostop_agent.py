"""Tests for the autostop agent's SSH resource monitor (minisky/autostop_agent.py)."""

from unittest.mock import MagicMock, patch

import pytest

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


class TestWatchUntilStopped:
    """
    watch_until_stopped() is what actually runs inside the detached
    autostop_runner.py process - it's the whole reason autostop can
    outlive the `minisky launch` command that started it (a
    threading.Thread dies the instant that command's process exits).
    """

    def test_loops_until_unregistered(self, tmp_path):
        agent = AutostopAgent(state=StateManager(db_path=str(tmp_path / "state.db")))

        calls = {"n": 0}

        def fake_check(vm_id):
            calls["n"] += 1
            if calls["n"] >= 3:
                agent.unregister(vm_id)

        agent._check_vm = fake_check
        agent._autostop_config.check_interval_seconds = 0

        agent.watch_until_stopped("mock-1", timeout_minutes=30)

        assert calls["n"] == 3
        assert "mock-1" not in agent._tracked_vms

    def test_exception_in_a_check_cycle_does_not_kill_the_loop(self, tmp_path):
        agent = AutostopAgent(state=StateManager(db_path=str(tmp_path / "state.db")))

        calls = {"n": 0}

        def flaky_check(vm_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash mid-check")
            agent.unregister(vm_id)

        agent._check_vm = flaky_check
        agent._autostop_config.check_interval_seconds = 0

        agent.watch_until_stopped("mock-1", timeout_minutes=30)  # must not raise

        assert calls["n"] == 2


class TestCheckVmUnregistersOnTerminalStatus:
    def test_stopped_vm_is_unregistered(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-1", "provider": "mock", "task_name": "t",
            "ip_address": "1.2.3.4", "status": "stopped",
        })
        agent = AutostopAgent(state=state)
        agent.register("mock-1", timeout_minutes=30)

        agent._check_vm("mock-1")

        assert "mock-1" not in agent._tracked_vms

    def test_terminated_vm_is_unregistered(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-1", "provider": "mock", "task_name": "t",
            "ip_address": "1.2.3.4", "status": "terminated",
        })
        agent = AutostopAgent(state=state)
        agent.register("mock-1", timeout_minutes=30)

        agent._check_vm("mock-1")

        assert "mock-1" not in agent._tracked_vms


class TestCheckTimestampIdleExceptionScope:
    """
    Regression tests for a real bug found while manually verifying the
    detached autostop watcher end-to-end: except (ValueError, TypeError)
    used to wrap the call to _handle_idle_vm() as well as the date
    parsing, so a UnicodeEncodeError raised deep inside it (printing an
    emoji on a non-UTF8 console - UnicodeEncodeError is itself a
    ValueError subclass) was silently swallowed. A VM correctly detected
    as idle would then just never actually get stopped, with nothing
    printed or logged anywhere to explain why.
    """

    def _agent_with_running_vm(self, tmp_path, idle_timeout_minutes=0):
        state = StateManager(db_path=str(tmp_path / "state.db"))
        state.add_vm({
            "vm_id": "mock-1", "provider": "mock", "task_name": "t",
            "ip_address": "1.2.3.4", "status": "running",
        })
        agent = AutostopAgent(
            state=state,
            autostop_config=AutostopConfig(idle_timeout_minutes=idle_timeout_minutes),
        )
        agent.register("mock-1", timeout_minutes=idle_timeout_minutes)
        vm_info = state.get_vm("mock-1")
        tracking = agent._tracked_vms["mock-1"]
        return agent, vm_info, tracking

    def test_failure_inside_handle_idle_vm_is_not_swallowed(self, tmp_path):
        agent, vm_info, tracking = self._agent_with_running_vm(tmp_path)

        with patch.object(agent, "_handle_idle_vm", side_effect=ValueError("simulated UnicodeEncodeError")):
            with pytest.raises(ValueError, match="simulated UnicodeEncodeError"):
                agent._check_timestamp_idle("mock-1", vm_info, tracking)

    def test_calls_handle_idle_vm_when_threshold_exceeded(self, tmp_path):
        agent, vm_info, tracking = self._agent_with_running_vm(tmp_path)

        with patch.object(agent, "_handle_idle_vm") as mock_handle:
            agent._check_timestamp_idle("mock-1", vm_info, tracking)

        mock_handle.assert_called_once()

    def test_malformed_timestamp_is_skipped_without_calling_handle_idle_vm(self, tmp_path):
        agent, _, tracking = self._agent_with_running_vm(tmp_path)
        vm_info = {"updated_at": "not-a-real-timestamp"}

        with patch.object(agent, "_handle_idle_vm") as mock_handle:
            agent._check_timestamp_idle("mock-1", vm_info, tracking)  # must not raise

        mock_handle.assert_not_called()
