"""
CLI integration tests for MiniSky.

Tests CLI commands using Typer's CliRunner to verify
command execution without launching real VMs.
"""

from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from typer.testing import CliRunner
from minisky.cli import app
from minisky.cli import _spawn_autostop_watcher as _real_spawn_autostop_watcher
from minisky.state import StateManager

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_state(tmp_path):
    """Replace global state manager with a temp one for every test."""
    db_path = str(tmp_path / "test_cli_state.db")
    temp_state = StateManager(db_path=db_path)
    with patch("minisky.cli.state", temp_state):
        yield temp_state


@pytest.fixture(autouse=True)
def mock_autostop_watcher():
    """
    Prevent `launch` from spawning a real detached OS process.

    autostop_minutes defaults to 30 in MiniSkyConfig, so since autostop
    registration is no longer gated on --detach, *every* successful
    `launch` in these tests would otherwise call subprocess.Popen(...)
    for real, spawning an actual `python -m minisky.autostop_runner`
    process as a side effect of running the test suite.
    """
    with patch("minisky.cli._spawn_autostop_watcher") as mock_spawn:
        mock_spawn.return_value = Path("/tmp/fake-autostop.log")
        yield mock_spawn


@pytest.fixture
def mock_config(tmp_path):
    """Replace global config with a temp one."""
    from minisky.config import MiniSkyConfig
    config_path = str(tmp_path / "test_config.yaml")
    temp_config = MiniSkyConfig(config_path=config_path)
    with patch("minisky.cli.config", temp_config):
        yield temp_config


@pytest.fixture
def populated_state(mock_state):
    """Populate state with sample VM entries."""
    mock_state.add_vm({
        "vm_id": "mock-abc12345",
        "ip_address": "127.0.0.1",
        "ssh_port": 22,
        "ssh_user": "root",
        "status": "running",
        "provider": "mock",
        "task_name": "test-task",
    })
    mock_state.add_vm({
        "vm_id": "mock-def67890",
        "ip_address": "127.0.0.2",
        "ssh_port": 22,
        "ssh_user": "root",
        "status": "stopped",
        "provider": "mock",
        "task_name": "stopped-task",
    })
    return mock_state


# ---------------------------------------------------------------------------
# Status command tests
# ---------------------------------------------------------------------------

class TestStatusCommand:
    """Test 'minisky status' command."""

    def test_status_no_vms(self, mock_state):
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No VMs found" in result.output

    def test_status_list_all(self, populated_state):
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "mock-abc12345" in result.output
        assert "mock-def67890" in result.output

    def test_status_specific_vm(self, populated_state):
        result = runner.invoke(app, ["status", "mock-abc12345"])
        assert result.exit_code == 0
        assert "mock-abc12345" in result.output
        assert "running" in result.output

    def test_status_vm_not_found(self, mock_state):
        result = runner.invoke(app, ["status", "nonexistent-vm"])
        assert result.exit_code == 1
        assert "VM not found" in result.output


# ---------------------------------------------------------------------------
# Terminate command tests
# ---------------------------------------------------------------------------

class TestTerminateCommand:
    """Test 'minisky terminate' command."""

    @patch("minisky.cli.get_provider")
    def test_terminate_with_force(self, mock_get_provider, populated_state):
        mock_provider = MagicMock()
        mock_get_provider.return_value = mock_provider

        result = runner.invoke(app, ["terminate", "mock-abc12345", "--force"])
        assert result.exit_code == 0
        assert "terminated" in result.output.lower()

        # VM should be removed from state
        assert populated_state.get_vm("mock-abc12345") is None

    def test_terminate_vm_not_found(self, mock_state):
        result = runner.invoke(app, ["terminate", "nonexistent", "--force"])
        assert result.exit_code == 1
        assert "VM not found" in result.output


# ---------------------------------------------------------------------------
# Stop/Start command tests
# ---------------------------------------------------------------------------

class TestStopStartCommands:
    """Test 'minisky stop' and 'minisky start' commands."""

    @patch("minisky.cli.get_provider")
    def test_stop_running_vm(self, mock_get_provider, populated_state):
        mock_provider = MagicMock()
        mock_provider.stop.return_value = True
        mock_get_provider.return_value = mock_provider

        result = runner.invoke(app, ["stop", "mock-abc12345"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

        # State should be updated
        vm = populated_state.get_vm("mock-abc12345")
        assert vm["status"] == "stopped"

    def test_stop_already_stopped(self, populated_state):
        result = runner.invoke(app, ["stop", "mock-def67890"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    @patch("minisky.cli.get_provider")
    def test_start_stopped_vm(self, mock_get_provider, populated_state):
        mock_provider = MagicMock()
        mock_provider.start.return_value = True
        mock_get_provider.return_value = mock_provider

        result = runner.invoke(app, ["start", "mock-def67890"])
        assert result.exit_code == 0
        assert "started" in result.output.lower()

        vm = populated_state.get_vm("mock-def67890")
        assert vm["status"] == "running"

    def test_start_already_running(self, populated_state):
        result = runner.invoke(app, ["start", "mock-abc12345"])
        assert result.exit_code == 0
        assert "not stopped" in result.output.lower()


# ---------------------------------------------------------------------------
# Exec command tests
# ---------------------------------------------------------------------------

class TestExecCommand:
    """Test 'minisky exec' command."""

    @patch("minisky.cli.Executor")
    def test_exec_success(self, MockExecutor, populated_state):
        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 0
        MockExecutor.return_value = mock_executor

        result = runner.invoke(app, ["exec", "mock-abc12345", "nvidia-smi"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower()
        mock_executor.connect.assert_called_once()
        mock_executor.disconnect.assert_called_once()

    @patch("minisky.cli.Executor")
    def test_exec_failure(self, MockExecutor, populated_state):
        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 1
        MockExecutor.return_value = mock_executor

        result = runner.invoke(app, ["exec", "mock-abc12345", "bad-command"])
        assert result.exit_code != 0

    def test_exec_vm_not_running(self, populated_state):
        result = runner.invoke(app, ["exec", "mock-def67890", "echo hi"])
        assert result.exit_code == 1
        assert "not running" in result.output.lower()


# ---------------------------------------------------------------------------
# Check command tests
# ---------------------------------------------------------------------------

class TestCheckCommand:
    """Test 'minisky check' command."""

    def test_check_runs(self, mock_config):
        result = runner.invoke(app, ["check"])
        assert result.exit_code == 0
        assert "mock" in result.output.lower()


# ---------------------------------------------------------------------------
# Launch command tests
# ---------------------------------------------------------------------------

class TestLaunchCommand:
    """Test 'minisky launch' command."""

    def test_launch_missing_file(self, mock_state):
        result = runner.invoke(app, ["launch", "nonexistent.yaml"])
        assert result.exit_code == 1

    @patch("minisky.cli.Provisioner")
    @patch("minisky.cli.get_provider")
    def test_launch_detach_mode(self, mock_get_provider, MockProvisioner, mock_state, tmp_path):
        """Launch in detach mode should not wait for SSH."""
        # Create a task YAML
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "name: test-detach\n"
            "provider: mock\n"
            "run:\n"
            "  - echo hello\n"
        )

        mock_provider = MagicMock()
        mock_provider.launch.return_value = {
            "vm_id": "mock-detach123",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "running",
            "provider": "mock",
            "task_name": "test-detach",
        }
        mock_get_provider.return_value = mock_provider

        result = runner.invoke(app, ["launch", str(task_yaml), "--detach"])
        assert result.exit_code == 0
        assert "detached" in result.output.lower()
        # VM should be in state
        assert mock_state.get_vm("mock-detach123") is not None

    @patch("minisky.cli.Provisioner")
    @patch("minisky.cli.get_provider")
    def test_autostop_registered_even_without_detach(
        self, mock_get_provider, MockProvisioner, mock_state, mock_autostop_watcher, tmp_path
    ):
        """Regression test: autostop registration used to be gated on
        --detach, so a synchronous `minisky launch` (no --detach) left
        the VM completely unprotected once the task finished - autostop
        must apply regardless of whether the command blocks or detaches."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "name: test-sync\n"
            "provider: mock\n"
            "run:\n"
            "  - echo hello\n"
        )

        mock_provider = MagicMock()
        mock_provider.launch.return_value = {
            "vm_id": "mock-sync123",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "running",
            "provider": "mock",
            "task_name": "test-sync",
        }
        mock_get_provider.return_value = mock_provider

        mock_provisioner = MockProvisioner.return_value
        mock_provisioner.wait_for_ssh.return_value = True
        mock_provisioner.run_task.return_value = (0, "hello")

        result = runner.invoke(app, ["launch", str(task_yaml)])  # no --detach

        assert result.exit_code == 0
        mock_autostop_watcher.assert_called_once()
        called_vm_id, called_timeout = mock_autostop_watcher.call_args.args
        assert called_vm_id == "mock-sync123"
        assert called_timeout == 30  # MiniSkyConfig's default autostop_minutes
        vm_info = mock_state.get_vm("mock-sync123")
        assert vm_info["autostop_minutes"] == 30

    @patch("minisky.cli.Provisioner")
    @patch("minisky.cli.get_provider")
    def test_no_autostop_flag_skips_registration(
        self, mock_get_provider, MockProvisioner, mock_state, mock_autostop_watcher, tmp_path
    ):
        """--no-autostop is the only way to opt out for a single launch -
        task.yaml's autostop_minutes field can't express 0/disabled since
        it's constrained to >= 1."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "name: test-no-autostop\n"
            "provider: mock\n"
            "run:\n"
            "  - echo hello\n"
        )

        mock_provider = MagicMock()
        mock_provider.launch.return_value = {
            "vm_id": "mock-noautostop1",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "running",
            "provider": "mock",
            "task_name": "test-no-autostop",
        }
        mock_get_provider.return_value = mock_provider

        mock_provisioner = MockProvisioner.return_value
        mock_provisioner.wait_for_ssh.return_value = True
        mock_provisioner.run_task.return_value = (0, "hello")

        result = runner.invoke(app, ["launch", str(task_yaml), "--no-autostop"])

        assert result.exit_code == 0
        mock_autostop_watcher.assert_not_called()
        vm_info = mock_state.get_vm("mock-noautostop1")
        assert "autostop_minutes" not in vm_info


class TestSpawnAutostopWatcher:
    """
    _spawn_autostop_watcher() must launch a genuinely detached OS
    process, not an in-process thread - a threading.Thread is killed the
    instant the CLI command that started it returns and the interpreter
    exits, so it never actually gets to watch anything past that single
    invocation. These tests mock subprocess.Popen itself (no real
    process spawned) and check it's invoked correctly.
    """

    def test_spawns_detached_process_with_correct_args(self, mock_config, tmp_path):
        mock_popen = MagicMock()
        with patch("subprocess.Popen", return_value=mock_popen) as popen_cls:
            log_path = _real_spawn_autostop_watcher("mock-abc123", 30)

        popen_cls.assert_called_once()
        cmd = popen_cls.call_args.args[0]
        assert cmd[1:4] == ["-m", "minisky.autostop_runner", "mock-abc123"]
        assert "--timeout-minutes" in cmd
        assert cmd[cmd.index("--timeout-minutes") + 1] == "30"
        assert log_path.name == "autostop-mock-abc123.log"

    def test_uses_windows_detachment_flags_on_win32(self, mock_config):
        import subprocess as subprocess_module

        # These constants only exist on subprocess when actually running
        # on win32 - fall back to their well-known literal values so this
        # test is meaningful (and doesn't AttributeError) on any host OS.
        DETACHED_PROCESS = getattr(subprocess_module, "DETACHED_PROCESS", 0x00000008)
        CREATE_NEW_PROCESS_GROUP = getattr(subprocess_module, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

        mock_popen = MagicMock()
        with patch("sys.platform", "win32"), \
             patch("subprocess.Popen", return_value=mock_popen) as popen_cls, \
             patch.object(subprocess_module, "DETACHED_PROCESS", DETACHED_PROCESS, create=True), \
             patch.object(subprocess_module, "CREATE_NEW_PROCESS_GROUP", CREATE_NEW_PROCESS_GROUP, create=True):
            _real_spawn_autostop_watcher("mock-abc123", 30)

        kwargs = popen_cls.call_args.kwargs
        assert kwargs["creationflags"] & DETACHED_PROCESS
        assert kwargs["creationflags"] & CREATE_NEW_PROCESS_GROUP

    def test_uses_new_session_on_posix(self, mock_config):
        mock_popen = MagicMock()
        with patch("sys.platform", "linux"), \
             patch("subprocess.Popen", return_value=mock_popen) as popen_cls:
            _real_spawn_autostop_watcher("mock-abc123", 30)

        assert popen_cls.call_args.kwargs["start_new_session"] is True
