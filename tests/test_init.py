"""
Tests for recost/_init.py
"""

import atexit
import subprocess
import sys
import textwrap
import threading


from recost._init import init
from recost._interceptor import is_installed
from recost._types import RecostConfig


class TestInit:
    def test_install_and_dispose(self):
        handle = init(RecostConfig(enabled=True))
        assert is_installed()
        handle.dispose()
        assert not is_installed()

    def test_disabled_does_not_install(self):
        handle = init(RecostConfig(enabled=False))
        assert not is_installed()
        handle.dispose()

    def test_double_init_disposes_first(self):
        init(RecostConfig())
        assert is_installed()
        h2 = init(RecostConfig())
        assert is_installed()
        h2.dispose()
        assert not is_installed()

    def test_dispose_is_idempotent(self):
        handle = init(RecostConfig())
        handle.dispose()
        handle.dispose()  # Should not raise
        assert not is_installed()


class TestLastFlushStatus:
    def test_none_before_any_flush(self):
        handle = init(RecostConfig())
        try:
            assert handle.last_flush_status is None
        finally:
            handle.dispose()

    def test_none_when_disabled(self):
        handle = init(RecostConfig(enabled=False))
        assert handle.last_flush_status is None
        handle.dispose()


class TestExcludePatterns:
    def test_cloud_mode_excludes_base_url(self):
        # We can't easily test the filtering without making real requests,
        # but we can verify init() doesn't crash with these settings
        handle = init(RecostConfig(
            api_key="test",
            project_id="proj",
            base_url="https://api.recost.dev",
            exclude_patterns=["/favicon.ico"],
        ))
        assert is_installed()
        handle.dispose()

    def test_local_mode_excludes_localhost(self):
        handle = init(RecostConfig(
            local_port=9999,
        ))
        assert is_installed()
        handle.dispose()


# ---------------------------------------------------------------------------
# Thread safety — init/dispose race
# ---------------------------------------------------------------------------


class TestInitDisposeRace:
    """Regression tests for issue #4 — concurrent init() / dispose() must
    not orphan handles or leave the SDK in an inconsistent state."""

    def _drain_module_state(self) -> None:
        """Ensure no prior test left a handle in place before we start."""
        from recost import _init as init_module

        if init_module._handle is not None:
            init_module._handle.dispose()
            init_module._handle = None

    def test_concurrent_init_does_not_orphan_handles(self):
        """N threads call init() concurrently. After all threads return,
        exactly one handle should be ``_handle`` (the last installer wins)
        and every other returned handle should be disposed by the
        next init() in line. Pre-fix the race lets two threads pass the
        ``_handle is None`` guard simultaneously and both install — the
        first thread's handle is then orphaned (not disposed)."""
        self._drain_module_state()

        from recost import _init as init_module
        from recost._init import init

        # Disabled config avoids actually starting transport / timer threads
        # so the test stays fast and isolated. The lock invariant we are
        # testing does not depend on those resources being live.
        def config_factory() -> RecostConfig:
            return RecostConfig(enabled=False)

        num_threads = 4
        iterations_per_thread = 20
        produced: list = []
        produced_lock = threading.Lock()
        exceptions: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    handle = init(config_factory())
                    with produced_lock:
                        produced.append(handle)
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        original_switch = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)
        try:
            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
        finally:
            sys.setswitchinterval(original_switch)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        # Exactly one handle should be undisposed — the final winner.
        undisposed = [h for h in produced if not h._disposed]
        assert len(undisposed) == 1, (
            f"expected exactly 1 undisposed handle, got {len(undisposed)} "
            f"out of {len(produced)} total — earlier handles were orphaned"
        )
        assert init_module._handle is undisposed[0], (
            "module-level _handle does not point at the active handle"
        )

        # Cleanup
        undisposed[0].dispose()

    def test_concurrent_init_dispose_leaves_module_clean(self):
        """N threads each loop init()/dispose() many times. After all
        threads finish, the module must report a clean state: ``_handle``
        is None and the interceptor is uninstalled."""
        self._drain_module_state()

        from recost import _init as init_module
        from recost._init import init
        from recost._interceptor import is_installed

        num_threads = 4
        iterations_per_thread = 20
        exceptions: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    handle = init(RecostConfig(enabled=False))
                    handle.dispose()
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        original_switch = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)
        try:
            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
        finally:
            sys.setswitchinterval(original_switch)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        assert init_module._handle is None, "module-level _handle should be None"
        assert not is_installed(), "interceptor should be uninstalled"


# ---------------------------------------------------------------------------
# Process lifecycle — atexit flush
# ---------------------------------------------------------------------------


class TestAtexitFlush:
    """Regression tests for issue #6 — a normal process exit (sys.exit,
    end of __main__, SIGTERM in a container) must flush the last
    aggregator bucket. Pre-fix the flush timer is a daemon thread, so
    process exit kills it and the last window is silently dropped."""

    def test_init_registers_atexit_when_enabled(self):
        """The atexit module exposes a list of registered callables only
        on CPython. We test by recording calls to a patched atexit.register
        instead of inspecting atexit internals — portable + behavioral."""
        from recost._init import init
        from recost._types import RecostConfig

        registered: list = []
        original_register = atexit.register

        def recording_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        atexit.register = recording_register  # type: ignore[assignment]
        try:
            handle = init(RecostConfig(enabled=True, auto_shutdown_handlers=True))
            handle.dispose()
        finally:
            atexit.register = original_register  # type: ignore[assignment]

        # At least one callable was registered by init().
        assert registered, "init() did not register an atexit callback"

    def test_init_skips_atexit_when_disabled(self):
        """auto_shutdown_handlers=False opts out of the registration."""
        from recost._init import init
        from recost._types import RecostConfig

        registered: list = []
        original_register = atexit.register

        def recording_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        atexit.register = recording_register  # type: ignore[assignment]
        try:
            handle = init(RecostConfig(enabled=True, auto_shutdown_handlers=False))
            handle.dispose()
        finally:
            atexit.register = original_register  # type: ignore[assignment]

        assert registered == [], (
            f"init() registered an atexit callback despite the opt-out: {registered!r}"
        )

    def test_subprocess_exit_flushes_final_bucket(self, tmp_path):
        """End-to-end: spawn a Python subprocess, init recost, generate one
        event, exit normally. Verify the transport's `send` was called —
        i.e. the atexit handler ran the final flush before the daemon
        thread died."""
        marker = tmp_path / "flush_marker.txt"

        # The child script monkey-patches Transport.send to write a marker
        # file when called, then init's recost with a tiny event, then
        # exits via sys.exit(0). If atexit works, the marker is written.
        script = textwrap.dedent(f"""
            import sys
            from recost._init import init
            from recost._transport import Transport
            from recost._types import RecostConfig

            original_send = Transport.send

            def patched_send(self, summary):
                with open({str(marker)!r}, "w") as f:
                    f.write("flushed:" + str(len(summary.metrics)))
                return original_send(self, summary)

            Transport.send = patched_send

            handle = init(RecostConfig(
                enabled=True,
                # Use a long flush_interval so only the atexit/final flush fires.
                flush_interval_ms=600_000,
                # Disable transport network IO via a fake api_key so cloud
                # mode hits our patched send (we don't care if HTTP fails).
                api_key="test",
                project_id="test",
                base_url="http://127.0.0.1:1",
            ))

            # Ingest one event directly through the interceptor's callback so
            # the aggregator has something to flush.
            from recost._types import RawEvent
            from recost._interceptor import _callback
            assert _callback is not None
            _callback(RawEvent(
                method="GET",
                url="https://api.openai.com/v1/chat/completions",
                host="api.openai.com",
                path="/v1/chat/completions",
                status_code=200,
                latency_ms=10,
                request_bytes=0,
                response_bytes=0,
                timestamp="2026-05-13T00:00:00Z",
            ))

            sys.exit(0)
        """).strip()

        script_path = tmp_path / "child.py"
        script_path.write_text(script)

        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"child exited {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert marker.exists(), (
            f"atexit final flush did not run. "
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        content = marker.read_text()
        assert content.startswith("flushed:"), (
            f"marker content unexpected: {content!r}"
        )


def test_handle_records_pid() -> None:
    """The handle records the PID at init time."""
    import os
    from recost import init, RecostConfig
    handle = init(RecostConfig(enabled=True, api_key=None))
    try:
        assert handle.pid == os.getpid()
    finally:
        handle.dispose()


def test_reinit_after_fork_resets_pid_and_threads() -> None:
    import os
    from recost import init, RecostConfig
    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        original_thread = handle._timer_thread
        # Simulate post-fork state: stale pid + dead thread reference
        handle.pid = 0  # impossible PID forces the rebuild branch
        handle.reinit_after_fork()
        assert handle._timer_thread is not original_thread
        assert handle._timer_thread is not None
        assert handle._timer_thread.is_alive()
        assert handle.pid == os.getpid()
    finally:
        handle.dispose()
