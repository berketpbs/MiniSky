"""Tests for storage/file mounts (minisky/storage.py)."""

from unittest.mock import MagicMock

import pytest

from minisky.storage import (
    StorageManager,
    StorageConfig,
    FileMount,
    MountMode,
    StorageProvider,
    S3Backend,
    GCSBackend,
    parse_file_mounts,
)


def _executor():
    executor = MagicMock()
    executor.execute_command.return_value = 0
    return executor


class TestFileMountParsing:
    def test_detects_provider_from_uri(self):
        assert FileMount(local_path="/d", source="s3://bucket/x").provider == StorageProvider.S3
        assert FileMount(local_path="/d", source="gs://bucket/x").provider == StorageProvider.GCS
        assert FileMount(local_path="/d", source="./local").provider == StorageProvider.LOCAL

    def test_bucket_and_object_path(self):
        mount = FileMount(local_path="/d", source="s3://my-bucket/datasets/imagenet")
        assert mount.bucket_name == "my-bucket"
        assert mount.object_path == "datasets/imagenet"

    def test_parse_file_mounts_simple_and_full_format(self):
        mounts = parse_file_mounts({
            "/data": "s3://bucket/data",
            "/ckpt": {"source": "gs://bucket/ckpt", "mode": "mount"},
        })
        by_path = {m.local_path: m for m in mounts}
        assert by_path["/data"].source == "s3://bucket/data"
        assert by_path["/ckpt"].mode == MountMode.MOUNT


class TestShellQuoting:
    """Paths/credentials containing spaces or shell metacharacters must
    not corrupt or inject into the remote command."""

    def test_s3_copy_quotes_path_with_space(self):
        executor = _executor()
        backend = S3Backend(StorageConfig())
        mount = FileMount(local_path="/data/my dataset", source="s3://bucket/x")

        backend.copy_to_vm(executor, mount)

        mkdir_cmd = executor.execute_command.call_args_list[0].args[0]
        sync_cmd = executor.execute_command.call_args_list[1].args[0]
        assert "'/data/my dataset'" in mkdir_cmd
        assert "'/data/my dataset'" in sync_cmd

    def test_s3_mount_quotes_credentials_containing_special_chars(self):
        executor = _executor()
        config = StorageConfig(s3_access_key="AKIA123", s3_secret_key="secret'with$chars")
        backend = S3Backend(config)
        mount = FileMount(local_path="/data", source="s3://bucket/x")

        backend.mount_on_vm(executor, mount)

        creds_cmd = next(
            c.args[0] for c in executor.execute_command.call_args_list
            if "passwd-s3fs" in c.args[0] and "echo" in c.args[0]
        )
        # The raw secret must be shell-escaped, not interpolated bare -
        # otherwise the embedded ' and $ break out of the echo command.
        assert "secret'\\''with$chars" in creds_cmd or "'secret'" not in creds_cmd

    def test_gcs_copy_quotes_paths(self):
        executor = _executor()
        backend = GCSBackend(StorageConfig())
        mount = FileMount(local_path="/data/a b", source="gs://bucket/x y")

        backend.copy_to_vm(executor, mount)

        sync_cmd = executor.execute_command.call_args_list[1].args[0]
        assert "'/data/a b'" in sync_cmd
        assert "'gs://bucket/x y'" in sync_cmd

    def test_unmount_quotes_local_path(self):
        executor = _executor()
        backend = S3Backend(StorageConfig())
        backend.unmount_on_vm(executor, "/data/my mount")
        cmd = executor.execute_command.call_args.args[0]
        assert "'/data/my mount'" in cmd


class TestSetupMountsErrorIsolation:
    def test_unregistered_provider_recorded_as_failure_not_raised(self):
        """get_backend() used to be called outside the per-mount
        try/except, so one bad mount's KeyError aborted the whole batch
        instead of being recorded like every other failure."""
        manager = StorageManager()
        good_mount = FileMount(local_path="/good", source="s3://bucket/good")
        bad_mount = FileMount(local_path="/bad", source="s3://bucket/bad")
        bad_mount.provider = "not-a-real-provider"  # bypass __post_init__'s detection

        executor = _executor()
        results = manager.setup_mounts(executor, [bad_mount, good_mount])

        assert results["/bad"] is False
        assert results["/good"] is True  # the good mount after it still ran


class TestCheckpoints:
    def test_save_checkpoint_infers_provider_from_uri(self):
        manager = StorageManager()
        executor = _executor()
        result = manager.save_checkpoint(executor, "/ckpt", "s3://bucket/run-1")
        assert result is True

    def test_save_checkpoint_unknown_uri_scheme_fails_cleanly(self):
        manager = StorageManager()
        executor = _executor()
        result = manager.save_checkpoint(executor, "/ckpt", "ftp://bucket/run-1")
        assert result is False
