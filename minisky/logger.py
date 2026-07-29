"""
Log streaming and persistence for MiniSky.

Provides real-time log streaming from remote VMs via SSH,
and local log file persistence under ~/.minisky/logs/.
"""

import time
import threading
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

import paramiko
from rich.console import Console

from .config import MiniSkyConfig

console = Console()


class LogManager:
    """
    Manages log streaming and persistence for VM tasks.

    Features:
    - Stream logs from remote VM via SSH (tail -f)
    - Save logs to local disk for later review
    - Follow mode for real-time output
    """

    def __init__(self, config: Optional[MiniSkyConfig] = None):
        """
        Initialize log manager.

        Args:
            config: MiniSky configuration instance
        """
        self._config = config or MiniSkyConfig()
        self._log_dir = self._config.log_dir
        self._active_streams: dict = {}

    def get_log_path(self, vm_id: str) -> Path:
        """
        Get the local log file path for a VM.

        Args:
            vm_id: VM identifier

        Returns:
            Path to the log file
        """
        return self._log_dir / f"{vm_id}.log"

    def write_log(self, vm_id: str, content: str):
        """
        Append content to a VM's local log file.

        Args:
            vm_id: VM identifier
            content: Log content to append
        """
        log_path = self.get_log_path(vm_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            for line in content.splitlines():
                f.write(f"[{timestamp}] {line}\n")

    def read_logs(self, vm_id: str, tail: int = 0) -> str:
        """
        Read saved logs for a VM.

        Args:
            vm_id: VM identifier
            tail: Number of last lines to return (0 = all)

        Returns:
            Log content as string
        """
        log_path = self.get_log_path(vm_id)
        if not log_path.exists():
            return ""

        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if tail > 0:
            lines = lines[-tail:]

        return ''.join(lines)

    def stream_logs(
        self,
        vm_info: dict,
        follow: bool = False,
        tail: int = 50,
        log_file: str = "/tmp/minisky_task.log",
        on_line: Optional[Callable[[str], None]] = None,
    ):
        """
        Stream logs from a remote VM via SSH.

        Args:
            vm_info: VM connection details (ip_address, ssh_port, ssh_user, ssh_key_path)
            follow: If True, keep streaming (tail -f). If False, show last N lines.
            tail: Number of lines to show initially
            log_file: Remote log file path to read
            on_line: Optional callback for each log line
        """
        ssh_client = None
        try:
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            hostname = vm_info['ip_address']
            port = vm_info.get('ssh_port', 22)
            username = vm_info.get('ssh_user', 'root')
            key_path = vm_info.get('ssh_key_path')

            connect_kwargs = {
                'hostname': hostname,
                'port': port,
                'username': username,
                'timeout': 15,
            }

            if key_path:
                key = paramiko.RSAKey.from_private_key_file(key_path)
                connect_kwargs['pkey'] = key
            else:
                connect_kwargs['look_for_keys'] = True

            ssh_client.connect(**connect_kwargs)

            if follow:
                cmd = f"tail -n {tail} -f {log_file} 2>/dev/null"
            else:
                cmd = f"tail -n {tail} {log_file} 2>/dev/null"

            stdin, stdout, stderr = ssh_client.exec_command(cmd)

            vm_id = vm_info.get('vm_id', 'unknown')

            for line in stdout:
                stripped = line.rstrip()
                if on_line:
                    on_line(stripped)
                else:
                    console.print(stripped)

                # Also persist locally
                self.write_log(vm_id, stripped)

            # Print any errors
            for line in stderr:
                stripped = line.rstrip()
                console.print(f"[red]{stripped}[/red]")

        except KeyboardInterrupt:
            console.print("\n[yellow]Log streaming stopped[/yellow]")
        except Exception as e:
            console.print(f"[red]Log streaming error:[/red] {str(e)}")
        finally:
            if ssh_client:
                ssh_client.close()

    def stream_logs_background(
        self,
        vm_info: dict,
        log_file: str = "/tmp/minisky_task.log",
    ) -> threading.Thread:
        """
        Start streaming logs in a background thread.

        Args:
            vm_info: VM connection details
            log_file: Remote log file path

        Returns:
            The background thread (daemon, auto-stops on main exit)
        """
        vm_id = vm_info.get('vm_id', 'unknown')

        def _stream():
            self.stream_logs(
                vm_info,
                follow=True,
                log_file=log_file,
                on_line=lambda line: self.write_log(vm_id, line),
            )

        thread = threading.Thread(target=_stream, daemon=True, name=f"log-{vm_id}")
        thread.start()
        self._active_streams[vm_id] = thread
        return thread
