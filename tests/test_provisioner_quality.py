import subprocess

import pytest

from minisky.provisioner import SSHKeyManager


def test_ssh_key_generation_decodes_non_utf8_errors(tmp_path, monkeypatch):
    manager = SSHKeyManager()
    monkeypatch.setattr("minisky.provisioner.Path.home", lambda: tmp_path)
    error = subprocess.CalledProcessError(
        1,
        ["ssh-keygen"],
        stderr=b"key generation failed \xff",
    )

    monkeypatch.setattr("minisky.provisioner.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(RuntimeError, match="key generation failed"):
        manager._generate_key()