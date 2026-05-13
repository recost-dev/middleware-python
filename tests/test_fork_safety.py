"""Fork-safety regression tests.

These use os.fork() so they only run on POSIX. Skip on Windows.
"""

import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.fork() is POSIX-only",
)


def test_register_at_fork_rebuilds_timer_in_child() -> None:
    """After fork(), the child should have a live timer thread, not the parent's stale reference."""
    from recost import init, RecostConfig

    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        parent_pid = os.getpid()
        assert handle._timer_thread is not None
        parent_thread_id = handle._timer_thread.ident

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            # Child
            os.close(read_fd)
            try:
                child_pid = os.getpid()
                # Allow the at_fork hook to fire
                time.sleep(0.1)
                # The hook should have updated handle.pid and built a new thread
                ok = (
                    handle.pid == child_pid
                    and handle._timer_thread is not None
                    and handle._timer_thread.is_alive()
                    and handle._timer_thread.ident != parent_thread_id
                )
                os.write(write_fd, b"1" if ok else b"0")
            finally:
                os.close(write_fd)
                os._exit(0)
        else:
            # Parent
            os.close(write_fd)
            os.waitpid(pid, 0)
            result = os.read(read_fd, 1)
            os.close(read_fd)
            assert result == b"1", "Child did not have a healthy reinit"
            # Parent's state untouched
            assert handle.pid == parent_pid
    finally:
        handle.dispose()
