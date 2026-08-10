"""
Tests for the log_streamer module.

Covers LogLine, LogBuffer, LogLevel, and SSHLogStreamer configuration.
"""

import pytest
import queue
import threading
import time
from datetime import datetime
from minisky.log_streamer import (
    LogLine,
    LogLevel,
    LogBuffer,
    SSHLogStreamer,
    stream_logs_cli,
)


# ---------------------------------------------------------------------------
# LogLine tests
# ---------------------------------------------------------------------------

class TestLogLine:
    """Test LogLine dataclass."""

    def test_defaults(self):
        line = LogLine(content="hello world")
        assert line.content == "hello world"
        assert line.level == LogLevel.STDOUT
        assert line.source == "task"
        assert line.vm_id is None
        assert isinstance(line.timestamp, datetime)

    def test_custom_level(self):
        line = LogLine(content="error!", level=LogLevel.ERROR, source="setup")
        assert line.level == LogLevel.ERROR
        assert line.source == "setup"

    def test_to_dict(self):
        line = LogLine(
            content="test output",
            level=LogLevel.INFO,
            source="task",
            vm_id="mock-abc123",
        )
        d = line.to_dict()
        assert d["content"] == "test output"
        assert d["level"] == "info"
        assert d["source"] == "task"
        assert d["vm_id"] == "mock-abc123"
        assert "timestamp" in d

    def test_format_rich(self):
        line = LogLine(content="formatted line", level=LogLevel.WARNING)
        text = line.format_rich()
        # Rich Text object should contain the content
        assert "formatted line" in str(text)

    def test_format_rich_with_source(self):
        line = LogLine(content="from setup", source="setup")
        text = line.format_rich()
        assert "setup" in str(text)


# ---------------------------------------------------------------------------
# LogLevel tests
# ---------------------------------------------------------------------------

class TestLogLevel:
    """Test LogLevel enum values."""

    def test_all_levels_exist(self):
        assert LogLevel.DEBUG == "debug"
        assert LogLevel.INFO == "info"
        assert LogLevel.WARNING == "warning"
        assert LogLevel.ERROR == "error"
        assert LogLevel.STDOUT == "stdout"
        assert LogLevel.STDERR == "stderr"


# ---------------------------------------------------------------------------
# LogBuffer tests
# ---------------------------------------------------------------------------

class TestLogBuffer:
    """Test thread-safe LogBuffer."""

    def test_append_and_get(self):
        buffer = LogBuffer()
        line1 = LogLine(content="line 1")
        line2 = LogLine(content="line 2")
        buffer.append(line1)
        buffer.append(line2)

        lines = buffer.get_lines()
        assert len(lines) == 2
        assert lines[0].content == "line 1"
        assert lines[1].content == "line 2"

    def test_tail(self):
        buffer = LogBuffer()
        for i in range(10):
            buffer.append(LogLine(content=f"line {i}"))

        tail = buffer.get_lines(tail=3)
        assert len(tail) == 3
        assert tail[0].content == "line 7"
        assert tail[2].content == "line 9"

    def test_max_size(self):
        buffer = LogBuffer(max_size=5)
        for i in range(10):
            buffer.append(LogLine(content=f"line {i}"))

        lines = buffer.get_lines()
        assert len(lines) == 5
        # Should keep the last 5
        assert lines[0].content == "line 5"
        assert lines[4].content == "line 9"

    def test_subscribe_and_receive(self):
        buffer = LogBuffer()
        sub_queue = buffer.subscribe()

        line = LogLine(content="new message")
        buffer.append(line)

        received = sub_queue.get(timeout=1)
        assert received.content == "new message"

    def test_unsubscribe(self):
        buffer = LogBuffer()
        sub_queue = buffer.subscribe()
        buffer.unsubscribe(sub_queue)

        buffer.append(LogLine(content="after unsub"))
        # Queue should be empty since we unsubscribed
        assert sub_queue.empty()

    def test_clear(self):
        buffer = LogBuffer()
        buffer.append(LogLine(content="data"))
        assert len(buffer.get_lines()) == 1

        buffer.clear()
        assert len(buffer.get_lines()) == 0

    def test_thread_safety(self):
        """Test concurrent writes from multiple threads."""
        buffer = LogBuffer()
        num_threads = 5
        lines_per_thread = 100

        def writer(thread_id):
            for i in range(lines_per_thread):
                buffer.append(LogLine(content=f"t{thread_id}-{i}"))

        threads = [
            threading.Thread(target=writer, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_lines = buffer.get_lines()
        assert len(all_lines) == num_threads * lines_per_thread

    def test_subscriber_full_queue_does_not_block(self):
        """When subscriber queue is full, append should not block."""
        buffer = LogBuffer()
        sub_queue = buffer.subscribe()

        # Fill beyond queue capacity (default 1000)
        for i in range(1100):
            buffer.append(LogLine(content=f"line {i}"))

        # Should not have raised or blocked
        assert True


# ---------------------------------------------------------------------------
# SSHLogStreamer configuration tests
# ---------------------------------------------------------------------------

class TestSSHLogStreamer:
    """Test SSHLogStreamer initialization and configuration."""

    def test_default_log_file(self):
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        streamer = SSHLogStreamer(vm_info)
        assert streamer.log_files == ["/tmp/minisky_task.log"]

    def test_custom_log_files(self):
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        streamer = SSHLogStreamer(vm_info, log_files=["/var/log/app.log", "/tmp/train.log"])
        assert len(streamer.log_files) == 2
        assert "/var/log/app.log" in streamer.log_files

    def test_custom_buffer(self):
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        custom_buffer = LogBuffer(max_size=100)
        streamer = SSHLogStreamer(vm_info, buffer=custom_buffer)
        assert streamer.buffer is custom_buffer

    def test_start_creates_threads(self):
        """Start should create daemon threads (we stop immediately)."""
        vm_info = {"ip_address": "10.0.0.1", "ssh_port": 22, "ssh_user": "root"}
        streamer = SSHLogStreamer(vm_info, log_files=["/tmp/test.log"])

        # Don't actually connect - just verify thread setup
        # We'll patch _stream_file to do nothing
        streamer._stream_file = lambda *args: None

        streamer.start()
        assert len(streamer._threads) == 1
        streamer.stop()
