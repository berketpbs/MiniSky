"""
Tests for the shared autostop-watcher spawning logic
(minisky/autostop_spawner.py), used by both cli.py (`minisky launch`)
and api/core.py's ClusterController (`minisky serve` / dashboard
launches) so a VM gets the same autostop protection regardless of
which path launched it.
"""

import subprocess
import sys
from unittest.mock import patch, MagicMock

from minisky.autostop_spawner import spawn_autostop_watcher
from minisky.config import MiniSkyConfig


def test_spawns_detached_process_with_correct_args(tmp_path):
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))
    mock_popen = MagicMock()

    with patch("subprocess.Popen", return_value=mock_popen) as popen_cls:
        log_path = spawn_autostop_watcher("mock-abc123", 30, config)

    popen_cls.assert_called_once()
    cmd = popen_cls.call_args.args[0]
    assert cmd[1:4] == ["-m", "minisky.autostop_runner", "mock-abc123"]
    assert "--timeout-minutes" in cmd
    assert cmd[cmd.index("--timeout-minutes") + 1] == "30"
    assert log_path.name == "autostop-mock-abc123.log"
    assert log_path.parent == config.log_dir


def test_uses_windows_detachment_flags_on_win32(tmp_path):
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))

    DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    mock_popen = MagicMock()
    with patch("sys.platform", "win32"), \
         patch("subprocess.Popen", return_value=mock_popen) as popen_cls, \
         patch.object(subprocess, "DETACHED_PROCESS", DETACHED_PROCESS, create=True), \
         patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", CREATE_NEW_PROCESS_GROUP, create=True):
        spawn_autostop_watcher("mock-abc123", 30, config)

    kwargs = popen_cls.call_args.kwargs
    assert kwargs["creationflags"] & DETACHED_PROCESS
    assert kwargs["creationflags"] & CREATE_NEW_PROCESS_GROUP


def test_uses_new_session_on_posix(tmp_path):
    config = MiniSkyConfig(config_path=str(tmp_path / "config.yaml"))
    mock_popen = MagicMock()

    with patch("sys.platform", "linux"), \
         patch("subprocess.Popen", return_value=mock_popen) as popen_cls:
        spawn_autostop_watcher("mock-abc123", 30, config)

    assert popen_cls.call_args.kwargs["start_new_session"] is True
