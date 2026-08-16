"""Tests for the shared console UTF-8 setup (minisky/console_utils.py)."""

import sys
from unittest.mock import MagicMock, patch

from minisky.console_utils import ensure_utf8_console


def test_noop_on_non_windows():
    with patch.object(sys, "platform", "linux"):
        ensure_utf8_console()  # must not touch stdout/stderr or raise


def test_reconfigures_stdout_and_stderr_on_windows():
    mock_stdout = MagicMock()
    mock_stderr = MagicMock()
    with patch.object(sys, "platform", "win32"), \
         patch.object(sys, "stdout", mock_stdout), \
         patch.object(sys, "stderr", mock_stderr):
        ensure_utf8_console()

    mock_stdout.reconfigure.assert_called_once_with(encoding="utf-8")
    mock_stderr.reconfigure.assert_called_once_with(encoding="utf-8")


def test_swallows_reconfigure_not_supported():
    mock_stdout = MagicMock()
    mock_stdout.reconfigure.side_effect = AttributeError("no reconfigure on this stream")
    with patch.object(sys, "platform", "win32"), patch.object(sys, "stdout", mock_stdout):
        ensure_utf8_console()  # must not raise
