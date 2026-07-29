"""
Real-time log streaming for MiniSky.

Provides advanced log streaming capabilities:
- Real-time SSH-based log tailing
- Multiple log source support
- Color-coded output
- WebSocket streaming for API
- Buffered output for performance
"""

import asyncio
import time
import threading
import queue
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any, AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import logging

import paramiko
from rich.console import Console
from rich.text import Text
from rich.live import Live
from rich.panel import Panel

logger = logging.getLogger(__name__)
console = Console()


class LogLevel(str, Enum):
    """Log level for color coding."""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass
class LogLine:
    """A single log line with metadata."""
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    level: LogLevel = LogLevel.STDOUT
    source: str = "task"
    vm_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level.value,
            "source": self.source,
            "vm_id": self.vm_id,
        }
    
    def format_rich(self) -> Text:
        """Format as Rich Text with colors."""
        text = Text()
        
        # Timestamp
        ts = self.timestamp.strftime("%H:%M:%S")
        text.append(f"[{ts}] ", style="dim")
        
        # Level indicator
        level_styles = {
            LogLevel.DEBUG: "dim",
            LogLevel.INFO: "cyan",
            LogLevel.WARNING: "yellow",
            LogLevel.ERROR: "red bold",
            LogLevel.STDOUT: "green",
            LogLevel.STDERR: "red",
        }
        style = level_styles.get(self.level, "white")
        
        # Source prefix
        if self.source != "task":
            text.append(f"[{self.source}] ", style="blue")
        
        # Content
        text.append(self.content, style=style)
        
        return text


class LogBuffer:
    """Thread-safe log buffer for collecting and distributing logs."""
    
    def __init__(self, max_size: int = 10000):
        self._buffer: List[LogLine] = []
        self._max_size = max_size
        self._lock = threading.Lock()
        self._subscribers: List[queue.Queue] = []
    
    def append(self, line: LogLine):
        """Add a log line to the buffer."""
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) > self._max_size:
                self._buffer = self._buffer[-self._max_size:]
            
            # Notify subscribers
            for q in self._subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass
    
    def get_lines(self, tail: int = 0) -> List[LogLine]:
        """Get log lines from buffer."""
        with self._lock:
            if tail > 0:
                return self._buffer[-tail:]
            return list(self._buffer)
    
    def subscribe(self) -> queue.Queue:
        """Subscribe to new log lines."""
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q
    
    def unsubscribe(self, q: queue.Queue):
        """Unsubscribe from log lines."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
    
    def clear(self):
        """Clear the buffer."""
        with self._lock:
            self._buffer.clear()


class SSHLogStreamer:
    """
    Streams logs from a remote VM via SSH.
    
    Supports:
    - Real-time tailing with tail -f
    - Multiple log files
    - Automatic reconnection
    - Graceful shutdown
    """
    
    def __init__(
        self,
        vm_info: Dict[str, Any],
        log_files: Optional[List[str]] = None,
        buffer: Optional[LogBuffer] = None,
    ):
        self.vm_info = vm_info
        self.log_files = log_files or ["/tmp/minisky_task.log"]
        self.buffer = buffer or LogBuffer()
        
        self._ssh_client: Optional[paramiko.SSHClient] = None
        self._running = False
        self._threads: List[threading.Thread] = []
    
    def _connect(self) -> paramiko.SSHClient:
        """Establish SSH connection."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        hostname = self.vm_info['ip_address']
        port = self.vm_info.get('ssh_port', 22)
        username = self.vm_info.get('ssh_user', 'root')
        key_path = self.vm_info.get('ssh_key_path')
        
        connect_kwargs = {
            'hostname': hostname,
            'port': port,
            'username': username,
            'timeout': 15,
        }
        
        if key_path:
            # Try different key types
            key = None
            key_path_obj = Path(key_path)
            
            try:
                key = paramiko.Ed25519Key.from_private_key_file(str(key_path_obj))
            except Exception:
                try:
                    key = paramiko.RSAKey.from_private_key_file(str(key_path_obj))
                except Exception:
                    try:
                        key = paramiko.ECDSAKey.from_private_key_file(str(key_path_obj))
                    except Exception:
                        logger.warning(f"Could not load key from {key_path}, trying without key")
            
            if key:
                connect_kwargs['pkey'] = key
            else:
                connect_kwargs['look_for_keys'] = True
        else:
            connect_kwargs['look_for_keys'] = True
        
        client.connect(**connect_kwargs)
        return client
    
    def _stream_file(self, log_file: str, source: str = "task"):
        """Stream a single log file."""
        vm_id = self.vm_info.get('vm_id', 'unknown')
        
        while self._running:
            try:
                client = self._connect()
                
                # Use tail -F (capital F) to follow file even if it's recreated
                cmd = f"tail -n 50 -F {log_file} 2>/dev/null"
                stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
                
                # Set non-blocking
                stdout.channel.setblocking(0)
                
                while self._running:
                    # Check if data is available
                    if stdout.channel.recv_ready():
                        data = stdout.channel.recv(4096).decode('utf-8', errors='replace')
                        for line in data.splitlines():
                            if line.strip():
                                log_line = LogLine(
                                    content=line.rstrip(),
                                    level=LogLevel.STDOUT,
                                    source=source,
                                    vm_id=vm_id,
                                )
                                self.buffer.append(log_line)
                    
                    # Check for stderr
                    if stdout.channel.recv_stderr_ready():
                        data = stdout.channel.recv_stderr(4096).decode('utf-8', errors='replace')
                        for line in data.splitlines():
                            if line.strip():
                                log_line = LogLine(
                                    content=line.rstrip(),
                                    level=LogLevel.STDERR,
                                    source=source,
                                    vm_id=vm_id,
                                )
                                self.buffer.append(log_line)
                    
                    # Small sleep to prevent CPU spinning
                    time.sleep(0.1)
                    
                    # Check if channel is closed
                    if stdout.channel.exit_status_ready():
                        break
                
                client.close()
                
            except Exception as e:
                logger.warning(f"Log streaming error for {log_file}: {e}")
                if self._running:
                    time.sleep(5)  # Wait before reconnecting
    
    def start(self):
        """Start streaming logs from all configured files."""
        self._running = True
        
        for log_file in self.log_files:
            source = Path(log_file).stem
            thread = threading.Thread(
                target=self._stream_file,
                args=(log_file, source),
                daemon=True,
                name=f"log-{source}"
            )
            thread.start()
            self._threads.append(thread)
    
    def stop(self):
        """Stop streaming."""
        self._running = False
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
    
    def stream_to_console(
        self,
        follow: bool = True,
        tail: int = 50,
        show_timestamps: bool = True,
    ):
        """
        Stream logs to console with rich formatting.
        
        Args:
            follow: Keep streaming (True) or show last N lines (False)
            tail: Number of initial lines to show
            show_timestamps: Whether to show timestamps
        """
        vm_id = self.vm_info.get('vm_id', 'unknown')
        
        try:
            client = self._connect()
            log_file = self.log_files[0] if self.log_files else "/tmp/minisky_task.log"
            
            if follow:
                cmd = f"tail -n {tail} -F {log_file} 2>/dev/null"
            else:
                cmd = f"tail -n {tail} {log_file} 2>/dev/null"
            
            stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
            
            console.print(f"[cyan]📋 Streaming logs from {vm_id}[/cyan]")
            console.print(f"[dim]   Log file: {log_file}[/dim]")
            console.print(f"[dim]   Press Ctrl+C to stop[/dim]\n")
            
            for line in stdout:
                stripped = line.rstrip()
                if stripped:
                    if show_timestamps:
                        ts = datetime.now().strftime("%H:%M:%S")
                        console.print(f"[dim][{ts}][/dim] {stripped}")
                    else:
                        console.print(stripped)
            
            # Print any errors
            for line in stderr:
                stripped = line.rstrip()
                if stripped:
                    console.print(f"[red]{stripped}[/red]")
            
            client.close()
            
        except KeyboardInterrupt:
            console.print("\n[yellow]Log streaming stopped[/yellow]")
        except Exception as e:
            console.print(f"[red]Log streaming error:[/red] {str(e)}")


class AsyncLogStreamer:
    """
    Async log streamer for API server WebSocket support.
    
    Provides async iteration over log lines for real-time
    streaming to WebSocket clients.
    """
    
    def __init__(
        self,
        vm_info: Dict[str, Any],
        log_file: str = "/tmp/minisky_task.log",
    ):
        self.vm_info = vm_info
        self.log_file = log_file
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    
    async def _stream_worker(self):
        """Background worker that streams logs."""
        import asyncssh
        
        vm_id = self.vm_info.get('vm_id', 'unknown')
        hostname = self.vm_info['ip_address']
        port = self.vm_info.get('ssh_port', 22)
        username = self.vm_info.get('ssh_user', 'root')
        key_path = self.vm_info.get('ssh_key_path')
        
        try:
            # Connect with asyncssh
            conn_kwargs = {
                'host': hostname,
                'port': port,
                'username': username,
                'known_hosts': None,  # Disable host key checking
            }
            
            if key_path:
                conn_kwargs['client_keys'] = [key_path]
            
            async with asyncssh.connect(**conn_kwargs) as conn:
                cmd = f"tail -n 50 -F {self.log_file} 2>/dev/null"
                
                async with conn.create_process(cmd) as process:
                    async for line in process.stdout:
                        if not self._running:
                            break
                        
                        log_line = LogLine(
                            content=line.rstrip(),
                            level=LogLevel.STDOUT,
                            source="task",
                            vm_id=vm_id,
                        )
                        
                        try:
                            self._queue.put_nowait(log_line)
                        except asyncio.QueueFull:
                            # Drop oldest if queue is full
                            try:
                                self._queue.get_nowait()
                                self._queue.put_nowait(log_line)
                            except asyncio.QueueEmpty:
                                pass
                        
        except ImportError:
            logger.warning("asyncssh not installed, falling back to sync streaming")
            # Fallback to sync streaming in thread
            await self._sync_fallback()
        except Exception as e:
            logger.error(f"Async log streaming error: {e}")
            await self._queue.put(LogLine(
                content=f"Error: {e}",
                level=LogLevel.ERROR,
                source="system",
            ))
    
    async def _sync_fallback(self):
        """Fallback to synchronous streaming in a thread."""
        def _stream():
            streamer = SSHLogStreamer(self.vm_info, [self.log_file])
            buffer = streamer.buffer
            sub_queue = buffer.subscribe()
            
            try:
                streamer.start()
                while self._running:
                    try:
                        line = sub_queue.get(timeout=1)
                        # Put into async queue (thread-safe)
                        asyncio.run_coroutine_threadsafe(
                            self._queue.put(line),
                            asyncio.get_event_loop()
                        )
                    except queue.Empty:
                        continue
            finally:
                streamer.stop()
                buffer.unsubscribe(sub_queue)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _stream)
    
    async def start(self):
        """Start the async streamer."""
        self._running = True
        asyncio.create_task(self._stream_worker())
    
    async def stop(self):
        """Stop the async streamer."""
        self._running = False
    
    async def __aiter__(self) -> AsyncIterator[LogLine]:
        """Async iterate over log lines."""
        while self._running:
            try:
                line = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield line
            except asyncio.TimeoutError:
                continue
            except Exception:
                break


def stream_logs_cli(
    vm_info: Dict[str, Any],
    follow: bool = True,
    tail: int = 50,
    log_file: str = "/tmp/minisky_task.log",
):
    """
    Convenience function for CLI log streaming.
    
    Args:
        vm_info: VM connection details
        follow: Keep streaming or show last N lines
        tail: Number of lines to show
        log_file: Remote log file path
    """
    streamer = SSHLogStreamer(vm_info, [log_file])
    streamer.stream_to_console(follow=follow, tail=tail)


def create_log_file_on_remote(
    vm_info: Dict[str, Any],
    log_file: str = "/tmp/minisky_task.log",
) -> bool:
    """
    Create/initialize log file on remote VM.
    
    Args:
        vm_info: VM connection details
        log_file: Path to create
    
    Returns:
        True if successful
    """
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
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
            connect_kwargs['look_for_keys'] = True
        
        client.connect(**connect_kwargs)
        
        # Create log file with header
        cmd = f"touch {log_file} && echo '=== MiniSky Task Log ===' >> {log_file}"
        stdin, stdout, stderr = client.exec_command(cmd)
        stdout.read()  # Wait for completion
        
        client.close()
        return True
        
    except Exception as e:
        logger.error(f"Failed to create log file: {e}")
        return False
