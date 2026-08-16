"""Tests for the job queue system (minisky/queue.py)."""

import time
import pytest

from minisky.queue import JobQueue, Job, JobStatus


@pytest.fixture
def queue(tmp_path):
    """Create a JobQueue backed by a temp SQLite database."""
    db_path = str(tmp_path / "test_jobs.db")
    return JobQueue(db_path=db_path)


class TestAddJob:
    def test_add_job_returns_job(self, queue):
        job = queue.add_job("vm-1", "echo hello")
        assert isinstance(job, Job)
        assert job.vm_id == "vm-1"
        assert job.command == "echo hello"
        assert job.status == JobStatus.PENDING
        assert job.job_id.startswith("job-")

    def test_add_job_with_metadata(self, queue):
        job = queue.add_job("vm-1", "python train.py", metadata={"epochs": 100})
        assert job.metadata == {"epochs": 100}

    def test_add_multiple_jobs(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-1", "cmd2")
        j3 = queue.add_job("vm-2", "cmd3")
        assert j1.job_id != j2.job_id != j3.job_id


class TestGetJob:
    def test_get_existing_job(self, queue):
        added = queue.add_job("vm-1", "echo test")
        retrieved = queue.get_job(added.job_id)
        assert retrieved is not None
        assert retrieved.job_id == added.job_id
        assert retrieved.command == "echo test"
        assert retrieved.status == JobStatus.PENDING

    def test_get_nonexistent_job(self, queue):
        assert queue.get_job("job-nonexistent-12345678") is None


class TestListJobs:
    def test_list_all_jobs(self, queue):
        queue.add_job("vm-1", "cmd1")
        queue.add_job("vm-1", "cmd2")
        queue.add_job("vm-2", "cmd3")
        jobs = queue.list_jobs()
        assert len(jobs) == 3

    def test_list_by_vm_id(self, queue):
        queue.add_job("vm-1", "cmd1")
        queue.add_job("vm-1", "cmd2")
        queue.add_job("vm-2", "cmd3")
        jobs = queue.list_jobs(vm_id="vm-1")
        assert len(jobs) == 2
        assert all(j.vm_id == "vm-1" for j in jobs)

    def test_list_by_status(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-1", "cmd2")
        queue.mark_running(j1.job_id)
        jobs = queue.list_jobs(status=JobStatus.RUNNING)
        assert len(jobs) == 1
        assert jobs[0].job_id == j1.job_id

    def test_list_with_limit(self, queue):
        for i in range(10):
            queue.add_job("vm-1", f"cmd{i}")
        jobs = queue.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_list_ordered_by_created_at_desc(self, queue):
        j1 = queue.add_job("vm-1", "first")
        j2 = queue.add_job("vm-1", "second")
        jobs = queue.list_jobs()
        # Most recent first
        assert jobs[0].job_id == j2.job_id


class TestGetPendingAndRunning:
    def test_get_pending_jobs(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-1", "cmd2")
        queue.mark_running(j1.job_id)
        pending = queue.get_pending_jobs("vm-1")
        assert len(pending) == 1
        assert pending[0].job_id == j2.job_id

    def test_get_running_jobs(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-2", "cmd2")
        queue.mark_running(j1.job_id)
        queue.mark_running(j2.job_id)
        running = queue.get_running_jobs()
        assert len(running) == 2

    def test_get_running_jobs_by_vm(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-2", "cmd2")
        queue.mark_running(j1.job_id)
        queue.mark_running(j2.job_id)
        running = queue.get_running_jobs(vm_id="vm-1")
        assert len(running) == 1


class TestUpdateStatus:
    def test_mark_running_sets_started_at(self, queue):
        job = queue.add_job("vm-1", "cmd")
        queue.mark_running(job.job_id)
        updated = queue.get_job(job.job_id)
        assert updated.status == JobStatus.RUNNING
        assert updated.started_at is not None

    def test_mark_completed(self, queue):
        job = queue.add_job("vm-1", "cmd")
        queue.mark_running(job.job_id)
        queue.mark_completed(job.job_id, exit_code=0, output="done")
        updated = queue.get_job(job.job_id)
        assert updated.status == JobStatus.COMPLETED
        assert updated.exit_code == 0
        assert updated.output == "done"
        assert updated.completed_at is not None

    def test_mark_failed(self, queue):
        job = queue.add_job("vm-1", "cmd")
        queue.mark_running(job.job_id)
        queue.mark_failed(job.job_id, exit_code=1, error="segfault")
        updated = queue.get_job(job.job_id)
        assert updated.status == JobStatus.FAILED
        assert updated.exit_code == 1
        assert updated.error == "segfault"

    def test_update_status_generic(self, queue):
        job = queue.add_job("vm-1", "cmd")
        result = queue.update_status(job.job_id, JobStatus.PENDING)
        # Generic update that doesn't set started_at or completed_at
        updated = queue.get_job(job.job_id)
        assert updated.status == JobStatus.PENDING


class TestCancelJob:
    def test_cancel_pending_job(self, queue):
        job = queue.add_job("vm-1", "cmd")
        assert queue.cancel_job(job.job_id) is True
        updated = queue.get_job(job.job_id)
        assert updated.status == JobStatus.CANCELLED

    def test_cancel_running_job_fails(self, queue):
        job = queue.add_job("vm-1", "cmd")
        queue.mark_running(job.job_id)
        assert queue.cancel_job(job.job_id) is False

    def test_cancel_nonexistent_job(self, queue):
        assert queue.cancel_job("job-nonexistent") is False


class TestRemoveJob:
    def test_remove_existing(self, queue):
        job = queue.add_job("vm-1", "cmd")
        assert queue.remove_job(job.job_id) is True
        assert queue.get_job(job.job_id) is None

    def test_remove_nonexistent(self, queue):
        assert queue.remove_job("job-nonexistent") is False


class TestClearVmJobs:
    def test_clear_all_vm_jobs(self, queue):
        queue.add_job("vm-1", "cmd1")
        queue.add_job("vm-1", "cmd2")
        queue.add_job("vm-2", "cmd3")
        count = queue.clear_vm_jobs("vm-1")
        assert count == 2
        assert len(queue.list_jobs(vm_id="vm-1")) == 0
        assert len(queue.list_jobs(vm_id="vm-2")) == 1

    def test_clear_vm_jobs_by_status(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-1", "cmd2")
        queue.mark_running(j1.job_id)
        count = queue.clear_vm_jobs("vm-1", status=JobStatus.PENDING)
        assert count == 1
        # Running job should still exist
        remaining = queue.list_jobs(vm_id="vm-1")
        assert len(remaining) == 1
        assert remaining[0].status == JobStatus.RUNNING


class TestGetStats:
    def test_stats_all(self, queue):
        j1 = queue.add_job("vm-1", "cmd1")
        j2 = queue.add_job("vm-1", "cmd2")
        j3 = queue.add_job("vm-1", "cmd3")
        queue.mark_running(j1.job_id)
        queue.mark_completed(j2.job_id)

        stats = queue.get_stats()
        assert stats["running"] == 1
        assert stats["completed"] == 1
        assert stats["pending"] == 1
        assert stats["total"] == 3

    def test_stats_by_vm(self, queue):
        queue.add_job("vm-1", "cmd1")
        queue.add_job("vm-2", "cmd2")
        stats = queue.get_stats(vm_id="vm-1")
        assert stats["pending"] == 1
        assert stats["total"] == 1

    def test_stats_empty(self, queue):
        stats = queue.get_stats()
        assert stats["total"] == 0


class TestJobModel:
    def test_to_dict_from_dict_roundtrip(self):
        job = Job(
            job_id="job-test-12345",
            vm_id="vm-1",
            command="echo hi",
            status=JobStatus.RUNNING,
            created_at=1000.0,
            started_at=1001.0,
            metadata={"key": "value"},
        )
        d = job.to_dict()
        assert d["status"] == "running"
        assert d["metadata"] == {"key": "value"}

        restored = Job.from_dict(d)
        assert restored.status == JobStatus.RUNNING
        assert restored.job_id == job.job_id

    def test_duration_running(self):
        job = Job(
            job_id="j", vm_id="v", command="c",
            status=JobStatus.RUNNING,
            created_at=1000.0,
            started_at=time.time() - 60,
        )
        assert job.duration is not None
        assert job.duration >= 59  # At least ~60 seconds

    def test_duration_completed(self):
        job = Job(
            job_id="j", vm_id="v", command="c",
            status=JobStatus.COMPLETED,
            created_at=1000.0,
            started_at=1000.0,
            completed_at=1060.0,
        )
        assert job.duration == 60.0

    def test_duration_pending(self):
        job = Job(
            job_id="j", vm_id="v", command="c",
            status=JobStatus.PENDING,
            created_at=1000.0,
        )
        assert job.duration is None

    def test_created_at_str(self):
        job = Job(
            job_id="j", vm_id="v", command="c",
            status=JobStatus.PENDING,
            created_at=0.0,
        )
        assert isinstance(job.created_at_str, str)
        assert len(job.created_at_str) > 0
