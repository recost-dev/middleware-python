"""
Tests for recost/_init.py
"""

import threading
import time

import pytest

from recost._init import init
from recost._interceptor import is_installed, uninstall
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
        h1 = init(RecostConfig())
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

import sys


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
        config_factory = lambda: RecostConfig(enabled=False)
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
