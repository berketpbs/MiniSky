"""Tests for the Provisioner's SSH key handling (minisky/provisioner.py)."""

from unittest.mock import patch

from minisky.provisioner import Provisioner

VM_INFO = {
    "vm_id": "i-0abc",
    "ip_address": "203.0.113.5",
    "ssh_port": 22,
    "ssh_user": "ubuntu",
}


class TestProvisionerKeySelection:
    def test_keeps_the_key_the_vm_was_launched_with(self):
        """
        Regression: the provisioner overwrote vm_info['ssh_key_path'] with
        MiniSky's own generated key, so a VM launched with a provider keypair
        (an EC2 .pem, say) authorised one key while setup and run offered
        another - `minisky exec` worked, `minisky launch` could not connect.
        """
        vm_info = {**VM_INFO, "ssh_key_path": "/home/user/.ssh/ec2-keypair.pem"}
        provisioner = Provisioner(vm_info)

        with patch.object(
            provisioner.ssh_key_manager, "get_key_path"
        ) as get_key_path:
            executor = provisioner._create_executor()

        assert executor.vm_info["ssh_key_path"] == "/home/user/.ssh/ec2-keypair.pem"
        get_key_path.assert_not_called()  # nor generated a key as a side effect

    def test_falls_back_to_miniskys_own_key(self, tmp_path):
        """No provider sets ssh_key_path today, so the fallback is the norm."""
        provisioner = Provisioner(dict(VM_INFO))
        generated = tmp_path / ".minisky" / "ssh" / "id_ed25519"

        with patch.object(
            provisioner.ssh_key_manager, "get_key_path", return_value=generated
        ):
            executor = provisioner._create_executor()

        assert executor.vm_info["ssh_key_path"] == str(generated)

    def test_an_empty_key_path_is_not_treated_as_a_key(self):
        """A provider that sets the field but leaves it blank means 'unset'."""
        provisioner = Provisioner({**VM_INFO, "ssh_key_path": ""})

        with patch.object(
            provisioner.ssh_key_manager, "get_key_path", return_value="/keys/minisky"
        ):
            executor = provisioner._create_executor()

        assert executor.vm_info["ssh_key_path"] == "/keys/minisky"

    def test_the_callers_vm_info_is_left_alone(self):
        """The executor gets a copy; the provisioner's own vm_info is untouched."""
        vm_info = dict(VM_INFO)
        provisioner = Provisioner(vm_info)

        with patch.object(
            provisioner.ssh_key_manager, "get_key_path", return_value="/keys/minisky"
        ):
            provisioner._create_executor()

        assert "ssh_key_path" not in vm_info
