"""Tests for the provider modules."""

import pytest
from minisky.providers import get_provider, register_provider, BaseProvider, ProviderError
from minisky.providers.mock import MockProvider
from minisky.task import Task


class TestProviderRegistry:
    """Tests for the provider registry."""

    def test_get_mock_provider(self):
        provider = get_provider('mock')
        assert isinstance(provider, MockProvider)

    def test_get_provider_case_insensitive(self):
        provider = get_provider('MOCK')
        assert isinstance(provider, MockProvider)

    def test_get_unknown_provider(self):
        with pytest.raises(ValueError) as exc_info:
            get_provider('nonexistent_cloud')
        assert "not found" in str(exc_info.value)

    def test_register_custom_provider(self):
        class CustomProvider(BaseProvider):
            def launch(self, task):
                return {}
            def status(self, vm_id):
                return {}
            def terminate(self, vm_id):
                return True
            def stop(self, vm_id):
                return True
            def start(self, vm_id):
                return True
            def list_instances(self):
                return []

        register_provider('custom', CustomProvider)
        provider = get_provider('custom')
        assert isinstance(provider, CustomProvider)

    def test_register_invalid_provider(self):
        with pytest.raises(TypeError):
            register_provider('bad', dict)


class TestMockProvider:
    """Tests for MockProvider."""

    @pytest.fixture
    def provider(self):
        return MockProvider({'simulate_delay': False})

    @pytest.fixture
    def sample_task(self):
        return Task(name="test-task", run=["echo hello"])

    def test_launch(self, provider, sample_task):
        vm_info = provider.launch(sample_task)
        assert 'vm_id' in vm_info
        assert vm_info['vm_id'].startswith('mock-')
        assert vm_info['ip_address'] == '127.0.0.1'
        assert vm_info['status'] == 'running'
        assert vm_info['provider'] == 'mock'
        assert vm_info['task_name'] == 'test-task'

    def test_status(self, provider, sample_task):
        vm_info = provider.launch(sample_task)
        status = provider.status(vm_info['vm_id'])
        assert status['status'] == 'running'

    def test_status_not_found(self, provider):
        with pytest.raises(ProviderError):
            provider.status('nonexistent')

    def test_terminate(self, provider, sample_task):
        vm_info = provider.launch(sample_task)
        result = provider.terminate(vm_info['vm_id'])
        assert result is True

    def test_terminate_not_found(self, provider):
        with pytest.raises(ProviderError):
            provider.terminate('nonexistent')

    def test_list_instances(self, provider, sample_task):
        assert provider.list_instances() == []
        provider.launch(sample_task)
        instances = provider.list_instances()
        assert len(instances) == 1

    def test_multiple_launches(self, provider, sample_task):
        vm1 = provider.launch(sample_task)
        vm2 = provider.launch(sample_task)
        assert vm1['vm_id'] != vm2['vm_id']
        instances = provider.list_instances()
        assert len(instances) == 2

    def test_validate_resources(self, provider, sample_task):
        assert provider.validate_resources(sample_task) is True
