"""Tests for the autostop runner module (minisky/autostop_runner.py)."""

import pytest
from unittest.mock import patch, MagicMock


class TestAutostopRunnerMain:
    """Tests for autostop_runner.main()."""

    @patch("minisky.autostop_runner.AutostopAgent")
    @patch("minisky.autostop_runner.StateManager")
    @patch("minisky.autostop_runner.MiniSkyConfig")
    def test_main_parses_args_and_calls_watch(self, mock_config_cls, mock_state_cls, mock_agent_cls):
        from minisky.autostop_runner import main

        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent

        result = main(["test-vm-123", "--timeout-minutes", "30"])

        assert result == 0
        mock_agent.watch_until_stopped.assert_called_once_with("test-vm-123", timeout_minutes=30)

    @patch("minisky.autostop_runner.AutostopAgent")
    @patch("minisky.autostop_runner.StateManager")
    @patch("minisky.autostop_runner.MiniSkyConfig")
    def test_main_custom_check_interval(self, mock_config_cls, mock_state_cls, mock_agent_cls):
        from minisky.autostop_runner import main

        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent

        result = main(["vm-xyz", "--timeout-minutes", "15", "--check-interval-seconds", "120"])

        assert result == 0
        # Verify AutostopConfig was created with custom interval
        call_kwargs = mock_agent_cls.call_args[1]
        assert call_kwargs["autostop_config"].check_interval_seconds == 120
        assert call_kwargs["autostop_config"].idle_timeout_minutes == 15

    @patch("minisky.autostop_runner.AutostopAgent")
    @patch("minisky.autostop_runner.StateManager")
    @patch("minisky.autostop_runner.MiniSkyConfig")
    def test_main_keyboard_interrupt(self, mock_config_cls, mock_state_cls, mock_agent_cls):
        from minisky.autostop_runner import main

        mock_agent = MagicMock()
        mock_agent.watch_until_stopped.side_effect = KeyboardInterrupt()
        mock_agent_cls.return_value = mock_agent

        result = main(["vm-abc", "--timeout-minutes", "10"])
        assert result == 0  # Should return 0 even on KeyboardInterrupt

    @patch("minisky.autostop_runner.AutostopAgent")
    @patch("minisky.autostop_runner.StateManager")
    @patch("minisky.autostop_runner.MiniSkyConfig")
    def test_main_creates_correct_config(self, mock_config_cls, mock_state_cls, mock_agent_cls):
        from minisky.autostop_runner import main

        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent

        main(["my-vm", "--timeout-minutes", "45"])

        # Verify AutostopAgent was instantiated with correct components
        mock_config_cls.assert_called_once()
        mock_state_cls.assert_called_once()
        mock_agent_cls.assert_called_once()
        call_kwargs = mock_agent_cls.call_args[1]
        assert "config" in call_kwargs
        assert "state" in call_kwargs
        assert "autostop_config" in call_kwargs

    def test_main_missing_required_args(self):
        from minisky.autostop_runner import main

        with pytest.raises(SystemExit) as exc_info:
            main([])  # Missing vm_id and --timeout-minutes
        assert exc_info.value.code != 0

    def test_main_missing_timeout(self):
        from minisky.autostop_runner import main

        with pytest.raises(SystemExit) as exc_info:
            main(["vm-id-only"])  # Missing --timeout-minutes
        assert exc_info.value.code != 0
