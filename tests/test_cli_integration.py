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
from minisky.cli import _find_catalog_entry
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


@pytest.fixture(autouse=True)
def mock_managed_job_runner():
    """Prevent `jobs launch` from spawning a real detached OS process."""
    with patch("minisky.cli._spawn_managed_job_runner") as mock_spawn:
        mock_spawn.return_value = Path("/tmp/fake-managed-job.log")
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


# ---------------------------------------------------------------------------
# Cost report command tests
# ---------------------------------------------------------------------------

class TestOptimizeCommand:
    """Test 'minisky optimize' command."""

    def test_optimize_with_gpu_task(self, mock_config, tmp_path):
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nresources:\n  gpu: A100\nrun:\n  - echo hi\n")

        result = runner.invoke(app, ["optimize", str(task_file)])

        assert result.exit_code == 0
        assert "mock" in result.output
        assert "Recommended" in result.output

    def test_optimize_missing_file(self, mock_config, tmp_path):
        result = runner.invoke(app, ["optimize", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1
        assert "Error" in result.output


class TestFindCatalogEntry:
    """
    Unit tests for _find_catalog_entry(), the provider/instance-type/GPU-name
    lookup used by cost-report to price each VM against GPUCatalog.fetch_all().
    """

    def test_matches_by_instance_type(self):
        entries = [
            {"provider": "aws", "instance_type": "p3.2xlarge", "gpu_name": "V100", "price_per_hour": 3.06},
            {"provider": "aws", "instance_type": "p4d.24xlarge", "gpu_name": "A100", "price_per_hour": 32.77},
        ]
        result = _find_catalog_entry(entries, "aws", instance_type="p3.2xlarge")
        assert result["price_per_hour"] == 3.06

    def test_instance_type_match_requires_same_provider(self):
        entries = [{"provider": "gcp", "instance_type": "p3.2xlarge", "price_per_hour": 1.0}]
        result = _find_catalog_entry(entries, "aws", instance_type="p3.2xlarge")
        assert result is None

    def test_falls_back_to_gpu_name_substring_match(self):
        entries = [{"provider": "mock", "gpu_name": "Mock A100 80GB", "price_per_hour": 0.0}]
        result = _find_catalog_entry(entries, "mock", instance_type=None, gpu_name="A100")
        assert result is not None
        assert result["gpu_name"] == "Mock A100 80GB"

    def test_gpu_name_match_is_case_insensitive(self):
        entries = [{"provider": "runpod", "gpu_name": "NVIDIA A100", "price_per_hour": 1.5}]
        result = _find_catalog_entry(entries, "runpod", gpu_name="a100")
        assert result is not None

    def test_no_match_returns_none(self):
        entries = [{"provider": "aws", "instance_type": "p3.2xlarge", "gpu_name": "V100"}]
        result = _find_catalog_entry(entries, "aws", instance_type="p4d.24xlarge", gpu_name="H100")
        assert result is None

    def test_no_instance_type_or_gpu_name_returns_none(self):
        entries = [{"provider": "aws", "instance_type": "p3.2xlarge", "gpu_name": "V100"}]
        result = _find_catalog_entry(entries, "aws")
        assert result is None


class TestCostReportCommand:
    """Test 'minisky cost-report' command."""

    def test_no_vms(self, mock_state, mock_config):
        result = runner.invoke(app, ["cost-report"])
        assert result.exit_code == 0
        assert "No VMs found" in result.output

    def test_does_not_crash_and_shows_free_mock_rate(self, mock_state, mock_config):
        """
        Regression test: cost-report used to unconditionally import a
        nonexistent PriceFetcher class and crash with ImportError on
        every invocation as soon as any VM existed in state.
        """
        mock_state.add_vm({
            "vm_id": "mock-abc12345",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "running",
            "provider": "mock",
            "task_name": "test-task",
            "resources": {"gpu": "A100", "gpu_count": 1},
        })

        result = runner.invoke(app, ["cost-report"])

        assert result.exit_code == 0
        assert "mock-abc1234" in result.output  # vm_id truncated to 12 chars
        assert "$0.000" in result.output  # mock catalog is free

    def test_stopped_vm_shows_zero_runtime_and_cost(self, mock_state, mock_config):
        mock_state.add_vm({
            "vm_id": "mock-stopped1",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "stopped",
            "provider": "mock",
            "task_name": "test-task",
            "resources": {"gpu": "A100", "gpu_count": 1},
        })

        result = runner.invoke(app, ["cost-report"])

        assert result.exit_code == 0
        assert "0.00" in result.output

    def test_uses_created_at_column_not_metadata(self, mock_state, mock_config):
        """
        Regression test: runtime must be derived from the `created_at`
        column StateManager stamps on every row, not a provider-supplied
        'launched_at' key that no provider ever actually sets.
        """
        mock_state.add_vm({
            "vm_id": "mock-abc12345",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "status": "running",
            "provider": "mock",
            "task_name": "test-task",
            "resources": {"gpu": "A100", "gpu_count": 1},
            "launched_at": None,
        })

        result = runner.invoke(app, ["cost-report"])

        assert result.exit_code == 0
        assert "Error" not in result.output

    def test_estimated_price_shown_with_marker_and_footnote(self, mock_state, mock_config):
        """AWS/GCP catalog entries carry price_is_estimate=True; cost-report
        should visually flag those rates as estimates, not live quotes."""
        mock_state.add_vm({
            "vm_id": "aws-i-12345678",
            "ip_address": "1.2.3.4",
            "ssh_port": 22,
            "ssh_user": "ubuntu",
            "status": "running",
            "provider": "aws",
            "task_name": "test-task",
            "instance_type": "p3.2xlarge",
        })

        fake_catalog = [{
            "provider": "aws",
            "instance_type": "p3.2xlarge",
            "gpu_name": "V100",
            "price_per_hour": 3.06,
            "price_is_estimate": True,
            "available": True,
        }]

        with patch("minisky.catalog.GPUCatalog.fetch_all", return_value=fake_catalog):
            result = runner.invoke(app, ["cost-report"])

        assert result.exit_code == 0
        assert "$3.060~" in result.output
        assert "static price estimate" in result.output


# ---------------------------------------------------------------------------
# Managed jobs (spot recovery) command tests
# ---------------------------------------------------------------------------

class TestJobsLaunchCommand:
    """Test 'minisky jobs launch' command."""

    def test_launch_submits_job_and_spawns_runner(self, tmp_path, mock_managed_job_runner):
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nresources:\n  gpu: A100\nrun:\n  - echo hi\n")

        result = runner.invoke(app, ["jobs", "launch", str(task_file)])

        assert result.exit_code == 0
        assert "Managed job submitted" in result.output
        mock_managed_job_runner.assert_called_once()
        job_id = mock_managed_job_runner.call_args.args[0]
        assert job_id.startswith("managed-")

    def test_launch_missing_file(self, tmp_path):
        result = runner.invoke(app, ["jobs", "launch", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_launch_no_run_commands(self, tmp_path):
        """Task itself enforces run has >=1 command; launch should surface that cleanly."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nrun: []\n")

        result = runner.invoke(app, ["jobs", "launch", str(task_file)])

        assert result.exit_code == 1
        assert "Error" in result.output


class TestJobsListStatusCommands:
    """Test 'minisky jobs list' and 'minisky jobs status'."""

    def test_list_no_jobs(self, mock_state):
        result = runner.invoke(app, ["jobs", "list"])
        assert result.exit_code == 0
        assert "No managed jobs found" in result.output

    def test_list_shows_submitted_job(self, tmp_path, mock_state):
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nresources:\n  gpu: A100\nrun:\n  - echo hi\n")
        runner.invoke(app, ["jobs", "launch", str(task_file)])

        result = runner.invoke(app, ["jobs", "list"])

        assert result.exit_code == 0
        assert "managed-" in result.output
        assert "pending" in result.output

    def test_status_not_found(self, mock_state):
        result = runner.invoke(app, ["jobs", "status", "managed-nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_status_shows_details(self, tmp_path, mock_state):
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nresources:\n  gpu: A100\nrun:\n  - echo hi\n")
        launch_result = runner.invoke(app, ["jobs", "launch", str(task_file)])
        job_id = None
        for line in launch_result.output.splitlines():
            if "Managed job submitted:" in line:
                job_id = line.split(":", 1)[1].strip()
        assert job_id is not None

        result = runner.invoke(app, ["jobs", "status", job_id])

        assert result.exit_code == 0
        assert job_id in result.output
        assert "pending" in result.output


class TestJobsCancelCommand:
    """Test 'minisky jobs cancel' command."""

    def test_cancel_not_found(self, mock_state):
        result = runner.invoke(app, ["jobs", "cancel", "managed-nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_cancel_pending_job(self, tmp_path, mock_state):
        task_file = tmp_path / "task.yaml"
        task_file.write_text("name: t\nresources:\n  gpu: A100\nrun:\n  - echo hi\n")
        launch_result = runner.invoke(app, ["jobs", "launch", str(task_file)])
        job_id = None
        for line in launch_result.output.splitlines():
            if "Managed job submitted:" in line:
                job_id = line.split(":", 1)[1].strip()
        assert job_id is not None

        result = runner.invoke(app, ["jobs", "cancel", job_id])

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

        status_result = runner.invoke(app, ["jobs", "status", job_id])
        assert "cancelled" in status_result.output.lower()
