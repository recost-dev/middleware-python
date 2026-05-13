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


def test_pid_backstop_triggers_reinit_when_hook_missing(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Simulate a fork that bypassed register_at_fork: handle.pid is stale, and
    the next intercepted event must trigger reinit_after_fork."""
    import os
    from recost import init, RecostConfig
    from recost._types import RawEvent

    # Block register_at_fork during init so the hook does NOT install.
    if hasattr(os, "register_at_fork"):
        monkeypatch.setattr(os, "register_at_fork", lambda **kw: None)

    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        original_thread = handle._timer_thread
        # Forge a PID mismatch (simulating: we are now in a forked child the hook didn't catch).
        handle.pid = 999_999

        # Drive the on_event path. The closure registered with install() is the entry point.
        from recost import _interceptor
        assert _interceptor._callback is not None
        _interceptor._callback(RawEvent(
            timestamp="2026-05-13T00:00:00Z",
            method="GET",
            url="https://example.invalid/",
            host="example.invalid",
            path="/",
            status_code=200,
            latency_ms=10.0,
            request_bytes=0,
            response_bytes=0,
        ))

        # The backstop should have rebuilt the handle in the current PID.
        assert handle.pid == os.getpid()
        assert handle._timer_thread is not original_thread
        assert handle._timer_thread is not None
        assert handle._timer_thread.is_alive()
    finally:
        handle.dispose()


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
