"""
Integration tests for MiniSky end-to-end workflows.

Tests the full lifecycle:
  launch → setup → run → terminate
using the mock provider and mocked SSH executor.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from minisky.task import Task, ResourceRequirements
from minisky.state import StateManager
from minisky.providers import get_provider
from minisky.providers.mock import MockProvider
from minisky.executor import Executor, ExecutorError
from minisky.provisioner import Provisioner, ProvisionConfig, ProvisionState, ProvisionResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_manager(tmp_path):
    """Create a temporary state manager for each test."""
    db_path = str(tmp_path / "test_state.db")
    return StateManager(db_path=db_path)


@pytest.fixture
def mock_provider(tmp_path):
    """Create a mock provider with no delays, isolated persisted state."""
    return MockProvider(config={"simulate_delay": False, "state_file": str(tmp_path / "mock_state.json")})


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
    return Task(
        name="integration-test",
        provider="mock",
        resources=ResourceRequirements(gpu="A100", gpu_count=1),
        setup=["pip install torch", "echo 'setup done'"],
        run=["python train.py --epochs 10"],
        env={"CUDA_VISIBLE_DEVICES": "0"},
    )


@pytest.fixture
def sample_task_no_setup():
    """Create a sample task without setup commands."""
    return Task(
        name="simple-test",
        provider="mock",
        run=["echo 'hello world'"],
    )


# ---------------------------------------------------------------------------
# End-to-end workflow tests
# ---------------------------------------------------------------------------

class TestLaunchWorkflow:
    """Test the complete launch → run → terminate workflow."""

    def test_launch_creates_vm(self, mock_provider, sample_task, state_manager):
        """Launching a task should create a VM and return valid info."""
        vm_info = mock_provider.launch(sample_task)

        assert vm_info["vm_id"].startswith("mock-")
        assert vm_info["ip_address"] == "127.0.0.1"
        assert vm_info["status"] == "running"
        assert vm_info["task_name"] == "integration-test"
        assert vm_info["provider"] == "mock"

    def test_launch_and_persist_state(self, mock_provider, sample_task, state_manager):
        """VM info should be persistable to state manager."""
        vm_info = mock_provider.launch(sample_task)
        state_manager.add_vm(vm_info)

        retrieved = state_manager.get_vm(vm_info["vm_id"])
        assert retrieved is not None
        assert retrieved["vm_id"] == vm_info["vm_id"]
        assert retrieved["ip_address"] == "127.0.0.1"
        assert retrieved["status"] == "running"

    def test_launch_status_terminate_cycle(self, mock_provider, sample_task, state_manager):
        """Full lifecycle: launch → status → terminate → verify removed."""
        # Launch
        vm_info = mock_provider.launch(sample_task)
        vm_id = vm_info["vm_id"]
        state_manager.add_vm(vm_info)

        # Check status
        status = mock_provider.status(vm_id)
        assert status["status"] == "running"

        # Terminate
        result = mock_provider.terminate(vm_id)
        assert result is True
        state_manager.remove_vm(vm_id)

        # Verify removed
        assert state_manager.get_vm(vm_id) is None

    def test_multiple_vms_lifecycle(self, mock_provider, state_manager):
        """Launch multiple VMs, verify all tracked, terminate all."""
        tasks = [
            Task(name=f"task-{i}", provider="mock", run=[f"echo {i}"])
            for i in range(5)
        ]

        vm_ids = []
        for task in tasks:
            vm_info = mock_provider.launch(task)
            state_manager.add_vm(vm_info)
            vm_ids.append(vm_info["vm_id"])

        # All should be listed
        all_vms = state_manager.list_vms()
        assert len(all_vms) == 5

        # Terminate all
        for vm_id in vm_ids:
            mock_provider.terminate(vm_id)
            state_manager.remove_vm(vm_id)

        assert len(state_manager.list_vms()) == 0

    def test_stop_start_lifecycle(self, mock_provider, sample_task, state_manager):
        """Test stop → start cycle preserves state."""
        vm_info = mock_provider.launch(sample_task)
        vm_id = vm_info["vm_id"]
        state_manager.add_vm(vm_info)

        # Stop
        mock_provider.stop(vm_id)
        state_manager.update_status(vm_id, "stopped")
        assert state_manager.get_vm(vm_id)["status"] == "stopped"

        # Start
        mock_provider.start(vm_id)
        state_manager.update_status(vm_id, "running")
        assert state_manager.get_vm(vm_id)["status"] == "running"


class TestProvisionerWorkflow:
    """Test the Provisioner state machine integration."""

    @patch("minisky.provisioner.Executor")
    def test_provision_state_transitions(self, MockExecutorClass, sample_task):
        """Provisioner should transition through correct states."""
        mock_executor = MagicMock()
        mock_executor.connect.return_value = True
        mock_executor.execute_command.return_value = 0
        MockExecutorClass.return_value = mock_executor

        vm_info = {
            "vm_id": "mock-test123",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
        }

        config = ProvisionConfig(ssh_timeout=5, ssh_retry_interval=1)
        provisioner = Provisioner(vm_info, config=config)

        # Override _check_port to always return True
        provisioner._check_port = MagicMock(return_value=True)

        assert provisioner.state == ProvisionState.PENDING

        # Wait for SSH
        result = provisioner.wait_for_ssh()
        assert result is True

        # Run setup
        success, output = provisioner.run_setup(["echo setup"])
        assert success is True
        assert provisioner.state == ProvisionState.RUNNING_SETUP

        # Run task
        exit_code, output = provisioner.run_task("python train.py")
        assert exit_code == 0
        assert provisioner.state == ProvisionState.COMPLETED

    @patch("minisky.provisioner.Executor")
    def test_provision_ssh_timeout(self, MockExecutorClass):
        """Provisioner should fail when SSH times out."""
        mock_executor = MagicMock()
        mock_executor.connect.side_effect = ExecutorError("Connection refused")
        MockExecutorClass.return_value = mock_executor

        vm_info = {
            "vm_id": "mock-timeout",
            "ip_address": "192.168.1.100",
            "ssh_port": 22,
            "ssh_user": "root",
        }

        config = ProvisionConfig(ssh_timeout=2, ssh_retry_interval=1)
        provisioner = Provisioner(vm_info, config=config)
        provisioner._check_port = MagicMock(return_value=True)

        result = provisioner.wait_for_ssh()
        assert result is False
        assert provisioner.state == ProvisionState.FAILED

    @patch("minisky.provisioner.Executor")
    def test_provision_setup_failure(self, MockExecutorClass):
        """Provisioner should handle setup command failures."""
        mock_executor = MagicMock()
        mock_executor.connect.return_value = True
        mock_executor.execute_command.return_value = 1  # Non-zero exit
        MockExecutorClass.return_value = mock_executor

        vm_info = {
            "vm_id": "mock-setupfail",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
        }

        config = ProvisionConfig(ssh_timeout=5, ssh_retry_interval=1)
        provisioner = Provisioner(vm_info, config=config)
        provisioner._check_port = MagicMock(return_value=True)

        provisioner.wait_for_ssh()
        success, output = provisioner.run_setup(["apt install nonexistent"])
        assert success is False
        assert provisioner.state == ProvisionState.FAILED

    @patch("minisky.provisioner.Executor")
    def test_provision_and_run_full(self, MockExecutorClass):
        """Test provision_and_run() complete lifecycle."""
        mock_executor = MagicMock()
        mock_executor.connect.return_value = True
        mock_executor.execute_command.return_value = 0
        mock_executor.disconnect.return_value = None
        MockExecutorClass.return_value = mock_executor

        vm_info = {
            "vm_id": "mock-full",
            "ip_address": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
        }

        config = ProvisionConfig(ssh_timeout=5, ssh_retry_interval=1)
        provisioner = Provisioner(vm_info, config=config)
        provisioner._check_port = MagicMock(return_value=True)

        result = provisioner.provision_and_run(
            setup_commands=["pip install torch"],
            run_command="python train.py",
            env={"CUDA_VISIBLE_DEVICES": "0"},
        )

        assert result.success is True
        assert result.state == ProvisionState.COMPLETED
        assert result.exit_code == 0
        mock_executor.disconnect.assert_called_once()


class TestStateConsistency:
    """Test that state remains consistent across operations."""

    def test_status_filter(self, state_manager, mock_provider):
        """Filtering VMs by status should return correct subsets."""
        task_running = Task(name="running-task", provider="mock", run=["echo 1"])
        task_stopped = Task(name="stopped-task", provider="mock", run=["echo 2"])

        vm1 = mock_provider.launch(task_running)
        state_manager.add_vm(vm1)

        vm2 = mock_provider.launch(task_stopped)
        state_manager.add_vm(vm2)
        state_manager.update_status(vm2["vm_id"], "stopped")

        running = state_manager.list_vms(status="running")
        stopped = state_manager.list_vms(status="stopped")

        assert len(running) == 1
        assert running[0]["task_name"] == "running-task"
        assert len(stopped) == 1
        assert stopped[0]["task_name"] == "stopped-task"

    def test_cleanup_terminated(self, state_manager, mock_provider):
        """Terminated VMs should be removable from state."""
        task = Task(name="temp-task", provider="mock", run=["echo x"])
        vm_info = mock_provider.launch(task)
        state_manager.add_vm(vm_info)
        vm_id = vm_info["vm_id"]

        state_manager.update_status(vm_id, "terminated")
        assert state_manager.get_vm(vm_id)["status"] == "terminated"

        state_manager.remove_vm(vm_id)
        assert state_manager.get_vm(vm_id) is None
