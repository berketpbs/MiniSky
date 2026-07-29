"""Tests for the task definition and YAML parser."""

import pytest
import yaml
from pathlib import Path
from minisky.task import Task, ResourceRequirements, FileMount


class TestResourceRequirements:
    """Tests for ResourceRequirements model."""

    def test_defaults(self):
        r = ResourceRequirements()
        assert r.gpu is None
        assert r.gpu_count == 1
        assert r.cpus is None
        assert r.memory_gb is None
        assert r.disk_gb == 50
        assert r.use_spot is False
        assert r.image_id is None

    def test_gpu_uppercase(self):
        r = ResourceRequirements(gpu="a100")
        assert r.gpu == "A100"

    def test_gpu_count_validation(self):
        with pytest.raises(Exception):
            ResourceRequirements(gpu_count=0)

    def test_disk_minimum(self):
        with pytest.raises(Exception):
            ResourceRequirements(disk_gb=5)

    def test_spot_enabled(self):
        r = ResourceRequirements(use_spot=True)
        assert r.use_spot is True


class TestFileMount:
    """Tests for FileMount model."""

    def test_default_mode(self):
        fm = FileMount(source="./data")
        assert fm.mode == "COPY"

    def test_mount_mode(self):
        fm = FileMount(source="s3://bucket/data", mode="mount")
        assert fm.mode == "MOUNT"

    def test_invalid_mode(self):
        with pytest.raises(Exception):
            FileMount(source="./data", mode="INVALID")


class TestTask:
    """Tests for Task model."""

    def test_minimal_task(self):
        t = Task(name="test", run=["echo hello"])
        assert t.name == "test"
        assert t.provider == "mock"
        assert t.num_nodes == 1
        assert t.run == ["echo hello"]

    def test_full_task(self):
        t = Task(
            name="full",
            provider="mock",
            resources=ResourceRequirements(gpu="H100", gpu_count=4, use_spot=True),
            run=["python train.py"],
            setup=["pip install torch"],
            env={"CUDA_VISIBLE_DEVICES": "0,1,2,3"},
            num_nodes=2,
            ports=[8080, 6006],
            autostop_minutes=30,
        )
        assert t.resources.gpu == "H100"
        assert t.resources.gpu_count == 4
        assert t.resources.use_spot is True
        assert t.num_nodes == 2
        assert t.ports == [8080, 6006]
        assert t.autostop_minutes == 30

    def test_provider_validation(self):
        with pytest.raises(Exception):
            Task(name="test", provider="invalid_cloud", run=["echo"])

    def test_provider_case_insensitive(self):
        t = Task(name="test", provider="MOCK", run=["echo"])
        assert t.provider == "mock"

    def test_run_required(self):
        with pytest.raises(Exception):
            Task(name="test", run=[])

    def test_from_yaml(self, tmp_path):
        yaml_content = {
            'name': 'yaml-test',
            'provider': 'mock',
            'resources': {'gpu': 'A100', 'gpu_count': 2},
            'run': ['python train.py'],
            'env': {'KEY': 'value'},
        }
        yaml_file = tmp_path / "task.yaml"
        with open(yaml_file, 'w') as f:
            yaml.dump(yaml_content, f)

        task = Task.from_yaml(str(yaml_file))
        assert task.name == 'yaml-test'
        assert task.resources.gpu == 'A100'
        assert task.resources.gpu_count == 2

    def test_from_yaml_with_file_mounts_shorthand(self, tmp_path):
        """String values in file_mounts should be treated as COPY sources."""
        yaml_content = {
            'name': 'mount-test',
            'run': ['echo ok'],
            'file_mounts': {
                '/data': './local_data',
                '/config': {'source': './configs', 'mode': 'COPY'},
            }
        }
        yaml_file = tmp_path / "task.yaml"
        with open(yaml_file, 'w') as f:
            yaml.dump(yaml_content, f)

        task = Task.from_yaml(str(yaml_file))
        assert '/data' in task.file_mounts
        assert task.file_mounts['/data'].source == './local_data'
        assert task.file_mounts['/data'].mode == 'COPY'
        assert task.file_mounts['/config'].source == './configs'

    def test_from_yaml_not_found(self):
        with pytest.raises(FileNotFoundError):
            Task.from_yaml("nonexistent.yaml")

    def test_from_yaml_empty(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        with pytest.raises(ValueError):
            Task.from_yaml(str(yaml_file))

    def test_to_yaml(self, tmp_path):
        task = Task(name="export-test", run=["echo ok"])
        yaml_file = tmp_path / "exported.yaml"
        task.to_yaml(str(yaml_file))

        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)
        assert data['name'] == 'export-test'
        assert data['run'] == ['echo ok']
