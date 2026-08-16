import pytest
from unittest.mock import MagicMock, patch
from minisky.executor import Executor, ExecutorError
from minisky.task import Task

@pytest.fixture
def mock_vm_info():
    return {
        'ip_address': '192.168.1.100',
        'ssh_port': 22,
        'ssh_user': 'root',
        'ssh_key_path': '/mock/key/path'
    }

@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_connect_success(mock_rsa, mock_ssh_client, mock_vm_info):
    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh_client.return_value = mock_ssh
    
    result = executor.connect(retries=1)
    
    assert result is True
    mock_ssh.connect.assert_called_once_with(
        hostname='192.168.1.100',
        port=22,
        username='root',
        pkey=mock_rsa.return_value,
        timeout=30
    )
    mock_ssh.open_sftp.assert_called_once()

@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_connect_failure(mock_rsa, mock_ssh_client, mock_vm_info):
    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh.connect.side_effect = Exception("Connection refused")
    mock_ssh_client.return_value = mock_ssh
    
    with pytest.raises(ExecutorError) as exc_info:
        executor.connect(retries=2)
    
    assert "Failed to connect after 2 attempts" in str(exc_info.value)

@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_execute_command(mock_rsa, mock_ssh_client, mock_vm_info):
    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh_client.return_value = mock_ssh
    executor.connect()
    
    # Mock exec_command returns
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.channel = mock_channel
    mock_stdout.__iter__.return_value = ["output line 1\n", "output line 2\n"]
    mock_stderr = MagicMock()
    mock_stderr.__iter__.return_value = []
    
    mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)
    
    # Run command with env and workdir
    env = {"TEST_VAR": "hello world"}
    exit_code = executor.execute_command(
        command="echo $TEST_VAR",
        env=env,
        workdir="~/workdir"
    )
    
    assert exit_code == 0
    # Check if the command was constructed properly with shlex and cd
    args, _ = mock_ssh.exec_command.call_args
    called_command = args[0]
    
    # Verify workdir chaining
    assert called_command.startswith("cd ~/workdir &&")
    # Verify environment quoting
    assert "TEST_VAR='hello world'" in called_command
    # Verify the actual command
    assert "echo $TEST_VAR" in called_command

@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_execute_command_on_line_callback_gets_each_line_tagged_by_stream(mock_rsa, mock_ssh_client, mock_vm_info):
    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh_client.return_value = mock_ssh
    executor.connect()

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.channel = mock_channel
    mock_stdout.__iter__.return_value = ["line 1\n", "line 2\n"]
    mock_stderr = MagicMock()
    mock_stderr.__iter__.return_value = ["warning: something\n"]

    mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

    received = []
    executor.execute_command("echo hi", on_line=lambda line, stream: received.append((line, stream)))

    assert received == [
        ("line 1", "stdout"),
        ("line 2", "stdout"),
        ("warning: something", "stderr"),
    ]


@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_execute_task_forwards_on_line_for_setup_and_run_commands(mock_rsa, mock_ssh_client, mock_vm_info):
    from minisky.task import Task

    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh_client.return_value = mock_ssh

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.channel = mock_channel
    mock_stdout.__iter__.return_value = ["ok\n"]
    mock_stderr = MagicMock()
    mock_stderr.__iter__.return_value = []
    mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

    task = Task(
        name="t",
        run=["python train.py"],
        setup=["pip install -r requirements.txt"],
    )

    received = []
    executor.execute_task(task, on_line=lambda line, stream: received.append((line, stream)))

    # Command markers for both phases, plus their (mocked) stdout output
    assert ("pip install -r requirements.txt", "command") in received
    assert ("python train.py", "command") in received
    assert received.count(("ok", "stdout")) == 2  # once per command


@patch('paramiko.SSHClient')
@patch('paramiko.RSAKey.from_private_key_file')
def test_sync_files(mock_rsa, mock_ssh_client, mock_vm_info, tmp_path):
    executor = Executor(mock_vm_info)
    mock_ssh = MagicMock()
    mock_ssh_client.return_value = mock_ssh
    executor.connect()
    
    # Mock SFTP
    mock_sftp = MagicMock()
    mock_sftp.normalize.return_value = "/home/root"
    executor.sftp_client = mock_sftp
    
    # Create a dummy local directory with tmp_path
    local_dir = tmp_path / "workdir"
    local_dir.mkdir()
    (local_dir / "test.txt").write_text("hello")
    
    # Call sync_files
    executor.sync_files(str(local_dir), "~/remote_workdir")
    
    # Ensure normalize was called due to ~/ path
    mock_sftp.normalize.assert_called_once_with('.')
    
    # Ensure a put request was made to the resolved home directory path
    mock_sftp.put.assert_called_once()
    args, _ = mock_sftp.put.call_args
    assert "/home/root/remote_workdir" in args[1]
