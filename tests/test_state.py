"""Tests for the state management module."""

import pytest
from minisky.state import StateManager


@pytest.fixture
def state_mgr(tmp_path):
    """Create a state manager with a temporary database."""
    db_path = str(tmp_path / "test_state.db")
    return StateManager(db_path=db_path)


@pytest.fixture
def sample_vm():
    """Sample VM info dict."""
    return {
        'vm_id': 'test-vm-001',
        'provider': 'mock',
        'task_name': 'unit-test',
        'ip_address': '192.168.1.100',
        'ssh_port': 22,
        'ssh_user': 'root',
        'status': 'running',
    }


class TestStateManager:
    """Tests for StateManager class."""

    def test_add_and_get_vm(self, state_mgr, sample_vm):
        state_mgr.add_vm(sample_vm)
        vm = state_mgr.get_vm('test-vm-001')
        assert vm is not None
        assert vm['vm_id'] == 'test-vm-001'
        assert vm['provider'] == 'mock'
        assert vm['ip_address'] == '192.168.1.100'
        assert vm['status'] == 'running'

    def test_get_missing_vm(self, state_mgr):
        vm = state_mgr.get_vm('nonexistent')
        assert vm is None

    def test_list_vms(self, state_mgr, sample_vm):
        state_mgr.add_vm(sample_vm)
        second = sample_vm.copy()
        second['vm_id'] = 'test-vm-002'
        second['task_name'] = 'second-task'
        state_mgr.add_vm(second)

        vms = state_mgr.list_vms()
        assert len(vms) == 2

    def test_list_vms_by_status(self, state_mgr, sample_vm):
        state_mgr.add_vm(sample_vm)
        stopped_vm = sample_vm.copy()
        stopped_vm['vm_id'] = 'test-vm-stopped'
        stopped_vm['status'] = 'stopped'
        state_mgr.add_vm(stopped_vm)

        running = state_mgr.list_vms(status='running')
        assert len(running) == 1
        assert running[0]['vm_id'] == 'test-vm-001'

    def test_update_status(self, state_mgr, sample_vm):
        state_mgr.add_vm(sample_vm)
        result = state_mgr.update_status('test-vm-001', 'stopped')
        assert result is True

        vm = state_mgr.get_vm('test-vm-001')
        assert vm['status'] == 'stopped'

    def test_update_status_missing(self, state_mgr):
        result = state_mgr.update_status('nonexistent', 'stopped')
        assert result is False

    def test_remove_vm(self, state_mgr, sample_vm):
        state_mgr.add_vm(sample_vm)
        result = state_mgr.remove_vm('test-vm-001')
        assert result is True
        assert state_mgr.get_vm('test-vm-001') is None

    def test_remove_missing_vm(self, state_mgr):
        result = state_mgr.remove_vm('nonexistent')
        assert result is False

    def test_metadata_storage(self, state_mgr):
        """Extra fields beyond core should be stored as metadata JSON."""
        vm = {
            'vm_id': 'meta-vm',
            'provider': 'mock',
            'task_name': 'meta-test',
            'ip_address': '10.0.0.1',
            'status': 'running',
            'custom_field': 'custom_value',
            'gpu_type': 'A100',
        }
        state_mgr.add_vm(vm)
        result = state_mgr.get_vm('meta-vm')
        assert result['custom_field'] == 'custom_value'
        assert result['gpu_type'] == 'A100'

    def test_empty_list(self, state_mgr):
        vms = state_mgr.list_vms()
        assert vms == []


class TestClusterPersistence:
    """Tests for the API server's cluster persistence methods."""

    def test_save_and_get_cluster(self, state_mgr):
        state_mgr.save_cluster("sky-1", {"cluster_id": "sky-1", "state": "up"})
        data = state_mgr.get_cluster_data("sky-1")
        assert data == {"cluster_id": "sky-1", "state": "up"}

    def test_get_missing_cluster(self, state_mgr):
        assert state_mgr.get_cluster_data("nonexistent") is None

    def test_save_cluster_upserts(self, state_mgr):
        state_mgr.save_cluster("sky-1", {"cluster_id": "sky-1", "state": "init"})
        state_mgr.save_cluster("sky-1", {"cluster_id": "sky-1", "state": "up"})
        assert state_mgr.get_cluster_data("sky-1")["state"] == "up"
        assert len(state_mgr.list_cluster_data()) == 1

    def test_list_cluster_data(self, state_mgr):
        state_mgr.save_cluster("sky-1", {"cluster_id": "sky-1"})
        state_mgr.save_cluster("sky-2", {"cluster_id": "sky-2"})
        assert len(state_mgr.list_cluster_data()) == 2

    def test_delete_cluster(self, state_mgr):
        state_mgr.save_cluster("sky-1", {"cluster_id": "sky-1"})
        assert state_mgr.delete_cluster("sky-1") is True
        assert state_mgr.get_cluster_data("sky-1") is None

    def test_delete_missing_cluster(self, state_mgr):
        assert state_mgr.delete_cluster("nonexistent") is False


class TestJobPersistence:
    """Tests for the API server's job persistence methods."""

    def test_save_and_get_job(self, state_mgr):
        state_mgr.save_job("job-1", {"job_id": "job-1", "state": "running"})
        data = state_mgr.get_job_data("job-1")
        assert data == {"job_id": "job-1", "state": "running"}

    def test_get_missing_job(self, state_mgr):
        assert state_mgr.get_job_data("nonexistent") is None

    def test_save_job_upserts(self, state_mgr):
        state_mgr.save_job("job-1", {"job_id": "job-1", "state": "pending"})
        state_mgr.save_job("job-1", {"job_id": "job-1", "state": "succeeded"})
        assert state_mgr.get_job_data("job-1")["state"] == "succeeded"
        assert len(state_mgr.list_job_data()) == 1

    def test_delete_job(self, state_mgr):
        state_mgr.save_job("job-1", {"job_id": "job-1"})
        assert state_mgr.delete_job("job-1") is True
        assert state_mgr.get_job_data("job-1") is None
