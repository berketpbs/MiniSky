"""Tests for multi-node cluster management (minisky/cluster.py)."""

from unittest.mock import MagicMock

import pytest

from minisky.cluster import ClusterManager, Cluster, ClusterNode, NodeRole


def _task(name="train"):
    task = MagicMock()
    task.name = name
    return task


def _vm(vm_id, ip="10.0.0.1"):
    return {"vm_id": vm_id, "ip_address": ip}


class TestClusterProperties:
    def test_head_and_worker_nodes(self):
        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=2)
        cluster.nodes = [
            ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0),
            ClusterNode(vm_id="w1", role=NodeRole.WORKER, ip_address="10.0.0.2", rank=1),
        ]
        assert cluster.head_node.vm_id == "head"
        assert [n.vm_id for n in cluster.worker_nodes] == ["w1"]
        assert cluster.is_ready is True

    def test_get_torchrun_cmd_appends_full_command_with_args(self):
        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=2)
        cluster.nodes = [ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0)]
        node = cluster.nodes[0]
        cmd = cluster.get_torchrun_cmd(node, "train.py --epochs 10")
        assert cmd.endswith("train.py --epochs 10")
        assert "--node_rank=0" in cmd


class TestCreateClusterPartialFailure:
    def test_cluster_trackable_after_worker_launch_fails_partway(self):
        state = MagicMock()
        state.add_vm = MagicMock()
        provider = MagicMock()
        # head succeeds, worker 1 fails
        provider.launch.side_effect = [_vm("head-vm"), RuntimeError("quota exceeded")]

        manager = ClusterManager(state, provider)

        with pytest.raises(RuntimeError):
            manager.create_cluster(_task(), num_nodes=3)

        # The cluster (with just the head node registered) must still be
        # reachable, instead of being an orphaned, untracked VM.
        clusters = manager.list_clusters()
        assert len(clusters) == 1
        cluster = clusters[0]
        assert len(cluster.nodes) == 1
        assert cluster.nodes[0].vm_id == "head-vm"

    def test_all_nodes_launch_successfully(self):
        state = MagicMock()
        provider = MagicMock()
        provider.launch.side_effect = [_vm("head-vm"), _vm("w1-vm"), _vm("w2-vm")]

        manager = ClusterManager(state, provider)
        cluster = manager.create_cluster(_task(), num_nodes=3)

        assert len(cluster.nodes) == 3
        assert cluster.head_node.vm_id == "head-vm"
        assert manager.get_cluster(cluster.cluster_id) is cluster


class TestRunDistributedTorchrun:
    def test_torchrun_used_even_when_command_has_cli_args(self):
        """command.endswith('.py') used to gate torchrun wrapping, which
        broke for any script invoked with arguments."""
        state = MagicMock()
        provider = MagicMock()
        manager = ClusterManager(state, provider)

        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=1)
        cluster.nodes = [ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0)]
        state.get_vm.return_value = _vm("head")

        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 0
        executor_factory = MagicMock(return_value=mock_executor)

        manager.run_distributed(cluster, "train.py --epochs 10", executor_factory, use_torchrun=True)

        called_cmd = mock_executor.execute_command.call_args.args[0]
        assert called_cmd.startswith("torchrun")
        assert called_cmd.endswith("train.py --epochs 10")

    def test_torchrun_skipped_when_not_requested(self):
        state = MagicMock()
        provider = MagicMock()
        manager = ClusterManager(state, provider)

        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=1)
        cluster.nodes = [ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0)]
        state.get_vm.return_value = _vm("head")

        mock_executor = MagicMock()
        mock_executor.execute_command.return_value = 0
        executor_factory = MagicMock(return_value=mock_executor)

        manager.run_distributed(cluster, "echo hi", executor_factory, use_torchrun=False)

        called_cmd = mock_executor.execute_command.call_args.args[0]
        assert called_cmd == "echo hi"


class TestTerminateClusterPartialFailure:
    def test_cluster_kept_when_a_node_fails_to_terminate(self):
        state = MagicMock()
        provider = MagicMock()
        provider.terminate.side_effect = [None, RuntimeError("timeout")]

        manager = ClusterManager(state, provider)
        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=2)
        cluster.nodes = [
            ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0),
            ClusterNode(vm_id="w1", role=NodeRole.WORKER, ip_address="10.0.0.2", rank=1),
        ]
        manager._clusters[cluster.cluster_id] = cluster

        success = manager.terminate_cluster(cluster)

        assert success is False
        assert manager.get_cluster(cluster.cluster_id) is cluster

    def test_cluster_removed_when_all_nodes_terminate(self):
        state = MagicMock()
        provider = MagicMock()

        manager = ClusterManager(state, provider)
        cluster = Cluster(cluster_id="c1", task_name="t", num_nodes=1)
        cluster.nodes = [ClusterNode(vm_id="head", role=NodeRole.HEAD, ip_address="10.0.0.1", rank=0)]
        manager._clusters[cluster.cluster_id] = cluster

        success = manager.terminate_cluster(cluster)

        assert success is True
        assert manager.get_cluster(cluster.cluster_id) is None
