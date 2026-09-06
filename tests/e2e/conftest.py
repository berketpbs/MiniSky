"""
A disposable stand-in for a cloud VM.

Most of MiniSky only runs over SSH - key auth, workdir sync, remote command
execution, log capture, detached runs - and none of it is exercised by mocked
unit tests. That gap is not theoretical: the suite was fully green while
SSH could not authenticate at all, because the tests asserted the broken
implementation rather than the behaviour.

These fixtures start a container running real sshd and hand back vm_info
pointing at it, so those paths can be tested end to end without a cloud
account. Everything is skipped cleanly when Docker is unavailable.
"""

import shutil
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

IMAGE = "minisky-e2e-sshd:latest"
CONTAINER = f"minisky-e2e-{uuid.uuid4().hex[:8]}"
DOCKERFILE_DIR = Path(__file__).parent


def _docker(*args, **kwargs):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, **kwargs
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return _docker("info", "--format", "{{.ServerVersion}}").returncode == 0


def _free_port() -> int:
    """Let the OS pick a port, so the fixture never collides with a real sshd."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


@pytest.fixture(scope="session")
def fake_vm():
    """
    A container running sshd, authorised with the same key the real launch
    path would use, yielded as a vm_info dict.

    Session-scoped: the image build and container start cost a few seconds,
    and the tests do not mutate it in ways that leak between them.
    """
    if not _docker_available():
        pytest.skip("Docker is not available; skipping SSH end-to-end tests")

    from minisky.provisioner import SSHKeyManager

    key_manager = SSHKeyManager()
    private_key = key_manager.get_key_path()
    public_key = key_manager.get_public_key()

    build = _docker("build", "-t", IMAGE, str(DOCKERFILE_DIR))
    if build.returncode != 0:
        pytest.skip(f"Could not build the test sshd image:\n{build.stderr[-2000:]}")

    port = _free_port()
    run = _docker(
        "run", "-d", "--name", CONTAINER,
        "-p", f"127.0.0.1:{port}:22", IMAGE,
    )
    if run.returncode != 0:
        pytest.skip(f"Could not start the test sshd container:\n{run.stderr[-2000:]}")

    try:
        if not _wait_for_port(port):
            pytest.skip("The test sshd container never started listening")

        # Install the key the same way a provider would, rather than baking a
        # developer-specific key into the image.
        install = _docker(
            "exec", CONTAINER, "bash", "-c",
            f"printf '%s\\n' {_shell_quote(public_key)} > /root/.ssh/authorized_keys "
            "&& chmod 600 /root/.ssh/authorized_keys",
        )
        assert install.returncode == 0, install.stderr

        yield {
            "vm_id": f"e2e-{CONTAINER}",
            "ip_address": "127.0.0.1",
            "ssh_port": port,
            "ssh_user": "root",
            "ssh_key_path": str(private_key),
            "status": "running",
            "provider": "mock",
        }
    finally:
        _docker("rm", "-f", CONTAINER)


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


@pytest.fixture
def clean_vm(fake_vm):
    """fake_vm with the task log and synced workdir cleared between tests."""
    _docker(
        "exec", CONTAINER, "bash", "-c",
        "rm -f /tmp/minisky_task.log; rm -rf /root/workdir /root/e2e-sync",
    )
    return fake_vm
