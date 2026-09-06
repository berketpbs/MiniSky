"""
End-to-end coverage for the half of MiniSky that only exists over SSH.

Every test here corresponds to something that was actually broken while the
mocked unit suite was green, so they are regression tests first and coverage
second. They need Docker; without it the whole module skips.

Run just these:      pytest tests/e2e
Skip them:           pytest tests -m "not e2e"
"""

import time

import pytest

from minisky.executor import Executor
from minisky.provisioner import ProvisionConfig, Provisioner

pytestmark = pytest.mark.e2e


def _read_remote(vm_info, path):
    executor = Executor(vm_info)
    executor.connect()
    try:
        _, stdout, _ = executor.ssh_client.exec_command(f"cat {path} 2>/dev/null")
        return stdout.read().decode()
    finally:
        executor.disconnect()


class TestSSHConnection:
    def test_connects_with_miniskys_own_key(self, clean_vm):
        """
        Regression: SSHKeyManager generates an ed25519 key while the executor
        parsed it as RSA, so authentication could never succeed on any cloud.
        """
        executor = Executor(clean_vm)
        try:
            assert executor.connect(retries=1) is True
        finally:
            executor.disconnect()

    def test_provisioner_waits_for_ssh(self, clean_vm):
        provisioner = Provisioner(
            clean_vm, config=ProvisionConfig(ssh_timeout=30, ssh_retry_interval=1)
        )
        assert provisioner.wait_for_ssh() is True


class TestRemoteExecution:
    def test_runs_a_command_and_reports_its_exit_code(self, clean_vm):
        executor = Executor(clean_vm)
        executor.connect()
        try:
            assert executor.execute_command("true", stream_output=False) == 0
            assert executor.execute_command("exit 42", stream_output=False) == 42
        finally:
            executor.disconnect()

    def test_output_reaches_the_log_minisky_logs_reads(self, clean_vm):
        """
        Regression: every reader referenced /tmp/minisky_task.log but nothing
        ever wrote it, so `minisky logs` could only ever show an empty file.
        """
        executor = Executor(clean_vm)
        executor.connect()
        try:
            executor.execute_command(
                "echo hello-from-the-task", stream_output=False,
                log_file="/tmp/minisky_task.log",
            )
        finally:
            executor.disconnect()

        assert "hello-from-the-task" in _read_remote(clean_vm, "/tmp/minisky_task.log")

    def test_logging_does_not_swallow_a_failure(self, clean_vm):
        """
        Regression: `cmd | tee` reports tee's status, so a failed task would
        have looked successful the moment logging was switched on.
        """
        executor = Executor(clean_vm)
        executor.connect()
        try:
            exit_code = executor.execute_command(
                "echo about-to-fail; exit 17", stream_output=False,
                log_file="/tmp/minisky_task.log",
            )
        finally:
            executor.disconnect()

        assert exit_code == 17
        assert "about-to-fail" in _read_remote(clean_vm, "/tmp/minisky_task.log")

    def test_stderr_is_captured_too(self, clean_vm):
        executor = Executor(clean_vm)
        executor.connect()
        try:
            executor.execute_command(
                "echo oops >&2", stream_output=False,
                log_file="/tmp/minisky_task.log",
            )
        finally:
            executor.disconnect()

        assert "oops" in _read_remote(clean_vm, "/tmp/minisky_task.log")


class TestWorkdirSync:
    def test_syncs_a_directory_over_sftp(self, clean_vm, tmp_path):
        (tmp_path / "train.py").write_text("print('hi')\n")
        (tmp_path / "data.txt").write_text("payload\n")

        executor = Executor(clean_vm)
        executor.connect()
        try:
            executor.sync_files(str(tmp_path), remote_path="/root/e2e-sync")
        finally:
            executor.disconnect()

        listing = _read_remote(clean_vm, "/root/e2e-sync/data.txt")
        assert "payload" in listing


class TestDetachedExecution:
    def test_task_outlives_the_ssh_session(self, clean_vm):
        """
        Regression: --detach returned before SSH was even established, so the
        task never ran at all and the VM just billed for nothing.
        """
        executor = Executor(clean_vm)
        executor.connect()
        try:
            pid = executor.execute_detached(
                "echo started; sleep 3; echo finished",
                log_file="/tmp/minisky_task.log",
            )
        finally:
            executor.disconnect()  # the task must survive this

        assert pid.isdigit()

        # Still running, with the session closed.
        early = _read_remote(clean_vm, "/tmp/minisky_task.log")
        assert "started" in early
        assert "finished" not in early

        deadline = time.time() + 20
        while time.time() < deadline:
            if "finished" in _read_remote(clean_vm, "/tmp/minisky_task.log"):
                break
            time.sleep(1)
        else:
            pytest.fail("detached task never finished")


class TestFullProvisionCycle:
    def test_setup_then_run_with_everything_logged(self, clean_vm):
        provisioner = Provisioner(
            clean_vm,
            config=ProvisionConfig(
                ssh_timeout=30, ssh_retry_interval=1, stream_logs=False
            ),
        )
        assert provisioner.wait_for_ssh() is True

        ok, _ = provisioner.run_setup(["echo setup-phase-ran"])
        assert ok is True

        exit_code, _ = provisioner.run_task("echo run-phase-ran")
        assert exit_code == 0

        log = _read_remote(clean_vm, "/tmp/minisky_task.log")
        assert "setup-phase-ran" in log
        assert "run-phase-ran" in log

    def test_failing_setup_is_reported_as_failure(self, clean_vm):
        provisioner = Provisioner(
            clean_vm,
            config=ProvisionConfig(
                ssh_timeout=30, ssh_retry_interval=1, stream_logs=False
            ),
        )
        assert provisioner.wait_for_ssh() is True

        ok, _ = provisioner.run_setup(["exit 3"])
        assert ok is False
