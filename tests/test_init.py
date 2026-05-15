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
            api_key="rc-test-key",
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
                # Must start with 'rc-' to pass api_key format validation.
                api_key="rc-test",
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


def test_init_rejects_string_undefined_as_api_key() -> None:
    """A literal 'undefined' (common config-file shape) must raise ValueError."""
    import pytest
    from recost import init, RecostConfig
    with pytest.raises(ValueError, match="must be a string beginning with 'rc-'"):
        init(RecostConfig(api_key="undefined"))


def test_init_rejects_non_string_api_key() -> None:
    import pytest
    from recost import init, RecostConfig
    with pytest.raises(ValueError, match="must be a string beginning with 'rc-'"):
        init(RecostConfig(api_key=123))  # type: ignore[arg-type]


def test_init_accepts_valid_rc_prefix() -> None:
    from recost import init, RecostConfig
    handle = init(RecostConfig(api_key="rc-abc123", project_id="p_1"))
    handle.dispose()


def test_init_accepts_none_api_key() -> None:
    from recost import init, RecostConfig
    handle = init(RecostConfig(api_key=None))
    handle.dispose()


def test_legacy_flush_interval_emits_deprecation_warning() -> None:
    """The legacy seconds-based flush_interval must still work but emit a
    DeprecationWarning at init() time."""
    import pytest as _pytest
    from recost import init, RecostConfig
    with _pytest.warns(DeprecationWarning, match="flush_interval is deprecated"):
        handle = init(RecostConfig(flush_interval=60.0, enabled=True))
    try:
        # The timer thread must still be alive — the legacy value was honored.
        assert handle._timer_thread is not None
        assert handle._timer_thread.is_alive()
    finally:
        handle.dispose()


def test_dispose_prevents_further_flushes(monkeypatch) -> None:
    """After dispose(), no new flush may be invoked on the transport — both
    the periodic timer and the atexit hook must be quiesced."""
    import time as _time
    from recost import init, RecostConfig
    from recost._transport import Transport

    send_count = [0]
    real_send = Transport.send

    def _counting_send(self, summary):
        send_count[0] += 1
        # Don't actually POST — base_url points at a port that isn't listening
        return real_send(self, summary)

    monkeypatch.setattr(Transport, "send", _counting_send)

    handle = init(RecostConfig(
        enabled=True,
        api_key="rc-test",
        project_id="p",
        base_url="http://127.0.0.1:1",
        flush_interval_ms=200,
        auto_shutdown_handlers=False,
    ))

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

    _time.sleep(0.5)
    pre_dispose = send_count[0]

    handle.dispose()
    send_count[0] = 0
    _time.sleep(0.6)

    assert send_count[0] == 0, (
        f"send was called {send_count[0]} times after dispose() — "
        f"timer was {pre_dispose} pre-dispose; dispose did not stop the loop"
    )


# ---------------------------------------------------------------------------
# Exclude semantics (#12)
# ---------------------------------------------------------------------------


class TestExcludeSemantics:
    """exclude_hosts (exact) + exclude_patterns (substring, no glob) +
    loopback parity in cloud mode pointed at localhost."""

    def test_exclude_patterns_rejects_asterisk(self):
        import pytest as _pytest
        from recost import init, RecostConfig
        with _pytest.raises(ValueError, match="substring match, not glob"):
            init(RecostConfig(exclude_patterns=["*.internal.corp"]))

    def test_exclude_hosts_exact_match(self, monkeypatch):
        from recost import init, RecostConfig
        from recost._transport import Transport
        from recost._types import RawEvent

        sent: list = []
        monkeypatch.setattr(Transport, "send", lambda self, s: sent.append(s))

        handle = init(RecostConfig(
            enabled=True,
            exclude_hosts=["api.example.com"],
            flush_interval_ms=600_000,
            auto_shutdown_handlers=False,
        ))
        from recost._interceptor import _callback
        assert _callback is not None

        def _ev(host: str) -> RawEvent:
            return RawEvent(
                timestamp="2026-05-14T00:00:00Z",
                method="GET",
                url=f"https://{host}/x",
                host=host,
                path="/x",
                status_code=200,
                latency_ms=10,
                request_bytes=0,
                response_bytes=0,
            )

        _callback(_ev("api.example.com"))    # excluded
        _callback(_ev("myapi.example.com"))  # NOT excluded — exact match
        handle.dispose()  # triggers final flush

        # myapi.example.com made it through; api.example.com did not
        assert len(sent) == 1
        total_events = sum(m.request_count for m in sent[0].metrics)
        assert total_events == 1

    def test_cloud_mode_excludes_base_url_host_only(self, monkeypatch):
        from recost import init, RecostConfig
        from recost._transport import Transport
        from recost._types import RawEvent

        sent: list = []
        monkeypatch.setattr(Transport, "send", lambda self, s: sent.append(s))

        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test",
            project_id="p",
            base_url="https://api.recost.dev",
            flush_interval_ms=600_000,
            auto_shutdown_handlers=False,
        ))
        from recost._interceptor import _callback
        assert _callback is not None
        # Exact host of base_url — excluded
        _callback(RawEvent(
            timestamp="2026-05-14T00:00:00Z",
            method="POST", url="https://api.recost.dev/projects/p/telemetry",
            host="api.recost.dev", path="/projects/p/telemetry",
            status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
        ))
        # Different host, base_url substring in URL — NOT excluded
        # (previously would have been falsely excluded by substring)
        _callback(RawEvent(
            timestamp="2026-05-14T00:00:00Z",
            method="GET",
            url="https://proxy.example.com/redirect?to=api.recost.dev",
            host="proxy.example.com", path="/redirect",
            status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
        ))
        handle.dispose()  # triggers final flush

        assert len(sent) == 1
        total_events = sum(m.request_count for m in sent[0].metrics)
        assert total_events == 1  # only the proxy.example.com event

    def test_cloud_mode_loopback_excludes_both_forms(self, monkeypatch):
        from recost import init, RecostConfig
        from recost._transport import Transport
        from recost._types import RawEvent

        sent: list = []
        monkeypatch.setattr(Transport, "send", lambda self, s: sent.append(s))

        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test",
            project_id="p",
            base_url="http://localhost:3000",
            flush_interval_ms=600_000,
            auto_shutdown_handlers=False,
        ))
        from recost._interceptor import _callback
        assert _callback is not None
        # Both loopback forms should be excluded
        for loopback in ("localhost", "127.0.0.1"):
            _callback(RawEvent(
                timestamp="2026-05-14T00:00:00Z",
                method="POST", url=f"http://{loopback}:3000/x",
                host=loopback, path="/x",
                status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
            ))
        # Different host should still be tracked
        _callback(RawEvent(
            timestamp="2026-05-14T00:00:00Z",
            method="GET", url="https://example.com/y",
            host="example.com", path="/y",
            status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
        ))
        handle.dispose()  # triggers final flush

        assert len(sent) == 1
        total_events = sum(m.request_count for m in sent[0].metrics)
        assert total_events == 1  # only example.com

    def test_local_mode_excludes_loopback_hosts(self, monkeypatch):
        from recost import init, RecostConfig
        from recost._transport import Transport
        from recost._types import RawEvent

        sent: list = []
        monkeypatch.setattr(Transport, "send", lambda self, s: sent.append(s))

        handle = init(RecostConfig(
            enabled=True,
            flush_interval_ms=600_000,
            auto_shutdown_handlers=False,
        ))
        from recost._interceptor import _callback
        assert _callback is not None
        for loopback in ("localhost", "127.0.0.1"):
            _callback(RawEvent(
                timestamp="2026-05-14T00:00:00Z",
                method="GET", url=f"http://{loopback}/x",
                host=loopback, path="/x",
                status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
            ))
        handle.dispose()  # triggers final flush

        # Aggregator empty -> no summary sent (or empty summary)
        assert sent == [] or all(
            sum(m.request_count for m in s.metrics) == 0 for s in sent
        )

    def test_local_mode_port_substring_excludes_other_loopback_urls(self, monkeypatch):
        """An event with host='not-localhost' but URL containing the local
        port should still be excluded by the :PORT substring pattern."""
        from recost import init, RecostConfig
        from recost._transport import Transport
        from recost._types import RawEvent

        sent: list = []
        monkeypatch.setattr(Transport, "send", lambda self, s: sent.append(s))

        handle = init(RecostConfig(
            enabled=True,
            local_port=9847,
            flush_interval_ms=600_000,
            auto_shutdown_handlers=False,
        ))
        from recost._interceptor import _callback
        assert _callback is not None
        _callback(RawEvent(
            timestamp="2026-05-14T00:00:00Z",
            method="GET", url="http://not-localhost:9847/x",
            host="not-localhost", path="/x",
            status_code=200, latency_ms=10, request_bytes=0, response_bytes=0,
        ))
        handle.dispose()  # triggers final flush

        assert sent == [] or all(
            sum(m.request_count for m in s.metrics) == 0 for s in sent
        )


# ---------------------------------------------------------------------------
# Flush-loop exponential backoff (#10)
# ---------------------------------------------------------------------------


class TestFlushLoopBackoff:
    """Persistent flush failures must back off, not fire every interval forever."""

    def test_consecutive_failures_below_threshold_no_backoff(self, monkeypatch):
        """Failures 1-4 should not emit any 'backing off' announcement."""
        import time as _time
        from recost import init, RecostConfig
        from recost._transport import Transport

        send_count = [0]

        def _failing_send(self, summary):
            send_count[0] += 1
            raise RuntimeError("simulated transport failure")

        monkeypatch.setattr(Transport, "send", _failing_send)

        errors: list = []
        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test", project_id="p",
            base_url="http://127.0.0.1:1",
            flush_interval_ms=30,
            on_error=errors.append,
            auto_shutdown_handlers=False,
        ))
        try:
            # Inject one event so flush_and_send actually tries to send
            from recost._types import RawEvent
            from recost._interceptor import _callback
            assert _callback is not None
            _callback(RawEvent(
                timestamp="2026-05-14T00:00:00Z", method="GET",
                url="https://api.openai.com/v1/x", host="api.openai.com",
                path="/v1/x", status_code=200, latency_ms=10,
                request_bytes=0, response_bytes=0,
            ))
            # Let several flush ticks fire (each tick re-ingests aggregator
            # is empty after first flush, so re-inject)
            for _ in range(3):
                _time.sleep(0.05)
                _callback(RawEvent(
                    timestamp="2026-05-14T00:00:00Z", method="GET",
                    url="https://api.openai.com/v1/x", host="api.openai.com",
                    path="/v1/x", status_code=200, latency_ms=10,
                    request_bytes=0, response_bytes=0,
                ))
            _time.sleep(0.05)
        finally:
            handle.dispose()

        # No "backing off" announcement should have fired yet (< 5 failures
        # per the typical timing — but be generous about timing slop)
        backoff_announcements = [
            e for e in errors
            if hasattr(e, "args") and e.args
            and isinstance(e.args[0], str) and "backing off" in e.args[0]
        ]
        # On slow CI hosts we might see 5+ flushes; in that case the test is
        # a no-op assertion of the underlying invariant. The strict claim:
        # if send_count < 5, there must be no backoff announcement.
        if send_count[0] < 5:
            assert backoff_announcements == [], (
                f"unexpected backoff announcement at "
                f"{send_count[0]} failures: {backoff_announcements}"
            )

    def test_fifth_failure_triggers_backoff_announcement(self, monkeypatch):
        """At the 5th consecutive failure, a 'backing off' RecostError fires."""
        import time as _time
        from recost import init, RecostConfig
        from recost._transport import Transport

        def _failing_send(self, summary):
            raise RuntimeError("simulated transport failure")

        monkeypatch.setattr(Transport, "send", _failing_send)
        # Make real sleeps a no-op when triggered via _defer
        # (the backoff feeds the _deferral_ms cell, not time.sleep — so
        # we don't need to patch time.sleep here)

        errors: list = []
        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test", project_id="p",
            base_url="http://127.0.0.1:1",
            flush_interval_ms=20,
            on_error=errors.append,
            auto_shutdown_handlers=False,
        ))
        try:
            from recost._types import RawEvent
            from recost._interceptor import _callback
            assert _callback is not None
            # Inject events repeatedly to keep the aggregator non-empty
            # across ticks (so each tick actually calls Transport.send).
            for _ in range(20):
                _callback(RawEvent(
                    timestamp="2026-05-14T00:00:00Z", method="GET",
                    url="https://api.openai.com/v1/x", host="api.openai.com",
                    path="/v1/x", status_code=200, latency_ms=10,
                    request_bytes=0, response_bytes=0,
                ))
                _time.sleep(0.025)
        finally:
            handle.dispose()

        backoff_announcements = [
            e for e in errors
            if hasattr(e, "args") and e.args
            and isinstance(e.args[0], str) and "backing off" in e.args[0]
        ]
        assert len(backoff_announcements) >= 1, (
            f"expected at least one 'backing off' announcement after "
            f"sustained failure, got errors: {[str(e) for e in errors]}"
        )
        # The first announcement should mention "5 consecutive" or higher
        first = str(backoff_announcements[0])
        assert "consecutive" in first
        assert "backing off" in first

    def test_successful_flush_resets_failure_counter(self, monkeypatch):
        """Counter resets on success — 4 fail + 1 success + 4 fail = no announce."""
        import time as _time
        from recost import init, RecostConfig
        from recost._transport import Transport

        call_count = [0]
        # Pattern: fail 4 times, succeed once, fail 4 times, succeed forever
        outcomes = ["fail"] * 4 + ["ok"] + ["fail"] * 4 + ["ok"] * 100

        def _pattern_send(self, summary):
            n = call_count[0]
            call_count[0] += 1
            if n < len(outcomes) and outcomes[n] == "fail":
                raise RuntimeError("simulated")
            return None

        monkeypatch.setattr(Transport, "send", _pattern_send)

        errors: list = []
        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test", project_id="p",
            base_url="http://127.0.0.1:1",
            flush_interval_ms=20,
            on_error=errors.append,
            auto_shutdown_handlers=False,
        ))
        try:
            from recost._types import RawEvent
            from recost._interceptor import _callback
            assert _callback is not None
            for _ in range(15):
                _callback(RawEvent(
                    timestamp="2026-05-14T00:00:00Z", method="GET",
                    url="https://api.openai.com/v1/x", host="api.openai.com",
                    path="/v1/x", status_code=200, latency_ms=10,
                    request_bytes=0, response_bytes=0,
                ))
                _time.sleep(0.03)
        finally:
            handle.dispose()

        backoff_announcements = [
            e for e in errors
            if hasattr(e, "args") and e.args
            and isinstance(e.args[0], str) and "backing off" in e.args[0]
        ]
        # The counter resets at the 5th send (success), so consecutive
        # failures stay at 4 max in the second window. No "backing off"
        # announcement should fire.
        assert backoff_announcements == [], (
            f"unexpected backoff announcement despite reset; "
            f"send pattern was {outcomes[:9]}, all errors: "
            f"{[str(e) for e in errors]}"
        )

    def test_backoff_announcement_fires_once_per_cap_doubling(self, monkeypatch):
        """Even under sustained failure, announcements are bounded by the
        number of distinct backoff levels — NOT one per flush tick."""
        import time as _time
        from recost import init, RecostConfig
        from recost._transport import Transport

        def _failing_send(self, summary):
            raise RuntimeError("simulated")

        monkeypatch.setattr(Transport, "send", _failing_send)

        errors: list = []
        handle = init(RecostConfig(
            enabled=True,
            api_key="rc-test", project_id="p",
            base_url="http://127.0.0.1:1",
            flush_interval_ms=15,
            on_error=errors.append,
            auto_shutdown_handlers=False,
        ))
        try:
            from recost._types import RawEvent
            from recost._interceptor import _callback
            assert _callback is not None
            # Run for ~1 second — that's 60+ flush ticks at 15ms.
            # Once backoff kicks in, the _deferral_ms cell pushes ticks
            # further out, so we don't actually get 60 send() calls.
            for _ in range(20):
                _callback(RawEvent(
                    timestamp="2026-05-14T00:00:00Z", method="GET",
                    url="https://api.openai.com/v1/x", host="api.openai.com",
                    path="/v1/x", status_code=200, latency_ms=10,
                    request_bytes=0, response_bytes=0,
                ))
                _time.sleep(0.05)
        finally:
            handle.dispose()

        backoff_announcements = [
            e for e in errors
            if hasattr(e, "args") and e.args
            and isinstance(e.args[0], str) and "backing off" in e.args[0]
        ]
        # Generous upper bound: at most one announcement per distinct
        # backoff level (1s, 2s, 4s, 8s, ..., 300s = 9 levels). On a real
        # 1-second window we'll see far fewer than that. The key invariant
        # is that this number is bounded and not growing linearly with
        # tick count.
        assert len(backoff_announcements) <= 9, (
            f"too many backoff announcements: {len(backoff_announcements)}; "
            f"should be at most one per cap doubling (≤9). "
            f"Announcements: {[str(e) for e in backoff_announcements]}"
        )
        # And there should be at least 1 (verifies the threshold fired)
        assert len(backoff_announcements) >= 1
