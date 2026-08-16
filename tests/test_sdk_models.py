"""Tests for the SDK's plain-data models (minisky/sdk/models.py)."""

from minisky.sdk.models import Cluster, Job, JobState, ClusterState


class TestJobIsTerminal:
    def test_failed_setup_is_terminal(self):
        """FAILED_SETUP exists specifically to mark a job that failed and
        won't progress - wait_until_complete() must not keep polling it."""
        job = Job(job_id="j1", name="n", state=JobState.FAILED_SETUP)
        assert job.is_terminal() is True

    def test_running_states_are_not_terminal(self):
        for state in (JobState.PENDING, JobState.SETTING_UP, JobState.RUNNING, JobState.RECOVERING):
            job = Job(job_id="j1", name="n", state=state)
            assert job.is_terminal() is False


class TestClusterFromDict:
    def test_round_trips_instance_type_and_accelerators(self):
        cluster = Cluster.from_dict({
            "cluster_id": "sky-1",
            "name": "c",
            "state": "up",
            "provider": "aws",
            "num_nodes": 1,
            "head_ip": "1.2.3.4",
            "instance_type": "g4dn.xlarge",
            "accelerators": {"T4": 1},
            "autostop_minutes": 30,
        })
        assert cluster.instance_type == "g4dn.xlarge"
        assert cluster.accelerators == {"T4": 1}
        assert cluster.autostop_minutes == 30
        assert cluster.state == ClusterState.UP
