"""
Tests for the SSH module (ssh.py).

Covers PortForward parsing, SSHConfig, SSHManager command building,
and common port lookups.
"""

import pytest
from minisky.ssh import (
    PortForward,
    SSHConfig,
    SSHManager,
    COMMON_PORTS,
    get_common_port,
    parse_port_forwards,
)


# ---------------------------------------------------------------------------
# PortForward tests
# ---------------------------------------------------------------------------

class TestPortForward:
    """Test PortForward dataclass and parsing."""

    def test_default_remote_port(self):
        pf = PortForward(local_port=8888)
        assert pf.remote_port == 8888
        assert pf.remote_host == "localhost"

    def test_explicit_remote_port(self):
        pf = PortForward(local_port=8080, remote_port=80)
        assert pf.local_port == 8080
        assert pf.remote_port == 80

    def test_to_ssh_arg(self):
        pf = PortForward(local_port=8888, remote_host="localhost", remote_port=8888)
        assert pf.to_ssh_arg() == "8888:localhost:8888"

    def test_to_ssh_arg_custom_host(self):
        pf = PortForward(local_port=3000, remote_host="10.0.0.1", remote_port=5000)
        assert pf.to_ssh_arg() == "3000:10.0.0.1:5000"

    def test_parse_single_port(self):
        pf = PortForward.parse("8888")
        assert pf.local_port == 8888
        assert pf.remote_port == 8888

    def test_parse_local_remote(self):
        pf = PortForward.parse("8080:80")
        assert pf.local_port == 8080
        assert pf.remote_port == 80

    def test_parse_full_spec(self):
        pf = PortForward.parse("8080:myhost:80")
        assert pf.local_port == 8080
        assert pf.remote_host == "myhost"
        assert pf.remote_port == 80

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid port forward spec"):
            PortForward.parse("a:b:c:d")


# ---------------------------------------------------------------------------
# SSHConfig tests
# ---------------------------------------------------------------------------

class TestSSHConfig:
    """Test SSHConfig defaults and configuration."""

    def test_defaults(self):
        config = SSHConfig(host="192.168.1.1")
        assert config.port == 22
        assert config.user == "root"
        assert config.key_path is None
        assert config.connect_timeout == 30
        assert config.server_alive_interval == 60
        assert config.strict_host_key_checking is False

    def test_custom_config(self):
        config = SSHConfig(
            host="10.0.0.1",
            port=2222,
            user="ubuntu",
            key_path="~/.ssh/id_ed25519",
            connect_timeout=60,
        )
        assert config.port == 2222
        assert config.user == "ubuntu"
        assert config.key_path == "~/.ssh/id_ed25519"


# ---------------------------------------------------------------------------
# SSHManager command building tests
# ---------------------------------------------------------------------------

class TestSSHManager:
    """Test SSHManager command construction."""

    @pytest.fixture
    def vm_info(self):
        return {
            "ip_address": "192.168.1.100",
            "ssh_port": 2222,
            "ssh_user": "ubuntu",
            "vm_id": "mock-test123",
        }

    @pytest.fixture
    def ssh_manager(self, vm_info):
        return SSHManager(vm_info)

    def test_basic_command(self, ssh_manager):
        cmd = ssh_manager._build_ssh_command()
        assert "ssh" in cmd
        assert "-p" in cmd
        assert "2222" in cmd
        assert "ubuntu@192.168.1.100" in cmd

    def test_command_with_remote_command(self, ssh_manager):
        cmd = ssh_manager._build_ssh_command(command="nvidia-smi")
        assert cmd[-1] == "nvidia-smi"

    def test_command_with_port_forwards(self, ssh_manager):
        forwards = [
            PortForward(local_port=8888),
            PortForward(local_port=6006),
        ]
        cmd = ssh_manager._build_ssh_command(port_forwards=forwards)
        assert "-L" in cmd
        assert "8888:localhost:8888" in cmd
        assert "6006:localhost:6006" in cmd

    def test_command_with_key_path(self, tmp_path):
        key_file = tmp_path / "test_key"
        key_file.write_text("fake key")

        vm_info = {
            "ip_address": "10.0.0.1",
            "ssh_port": 22,
            "ssh_user": "root",
            "ssh_key_path": str(key_file),
        }
        manager = SSHManager(vm_info)
        cmd = manager._build_ssh_command()
        assert "-i" in cmd
        assert str(key_file) in cmd

    def test_strict_host_key_disabled(self, ssh_manager):
        cmd = ssh_manager._build_ssh_command()
        assert "StrictHostKeyChecking=no" in " ".join(cmd)
        assert "UserKnownHostsFile=/dev/null" in " ".join(cmd)

    def test_extra_args(self, ssh_manager):
        cmd = ssh_manager._build_ssh_command(extra_args=["-N", "-f"])
        assert "-N" in cmd
        assert "-f" in cmd


# ---------------------------------------------------------------------------
# Common ports tests
# ---------------------------------------------------------------------------

class TestCommonPorts:
    """Test common port lookups and parsing."""

    def test_jupyter_port(self):
        pf = get_common_port("jupyter")
        assert pf is not None
        assert pf.local_port == 8888

    def test_tensorboard_port(self):
        pf = get_common_port("tensorboard")
        assert pf is not None
        assert pf.local_port == 6006

    def test_gradio_port(self):
        pf = get_common_port("gradio")
        assert pf is not None
        assert pf.local_port == 7860

    def test_unknown_port(self):
        pf = get_common_port("unknown_service")
        assert pf is None

    def test_case_insensitive(self):
        pf = get_common_port("JUPYTER")
        assert pf is not None

    def test_parse_port_forwards_mixed(self):
        """Parse a mix of named and numeric port specs."""
        forwards = parse_port_forwards(["jupyter", "6006", "3000:80"])
        assert len(forwards) == 3
        assert forwards[0].local_port == 8888  # jupyter
        assert forwards[1].local_port == 6006
        assert forwards[2].local_port == 3000
        assert forwards[2].remote_port == 80

    def test_all_common_ports_have_values(self):
        """All common port entries should have valid port numbers."""
        for name, pf in COMMON_PORTS.items():
            assert pf.local_port > 0
            assert pf.remote_port > 0
