"""
CLI integration tests for MiniSky.

Tests CLI commands using Typer's CliRunner to verify
command execution without launching real VMs.
"""

import pytest
from unittest.mock import patch, MagicMock
from typer.testing import CliRunner
from minisky.cli import app
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
