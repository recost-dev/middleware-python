# Module-Level State Races Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the two module-level state races documented in [issue #4](https://github.com/recost-dev/middleware-python/issues/4) by introducing one `threading.RLock` per affected module. (1) In `recost/_interceptor.py`, race `install()` and `uninstall()` so the patches never end up half-applied or with a wrapper outliving `_installed = False`. (2) In `recost/_init.py`, race `init()` and `dispose()` so the module-level `_handle` global can never be in an inconsistent state and previous handles can't be silently orphaned.

**Architecture:** Two independent RLocks — one in each module. The locks are independent (no cross-module locking discipline) because `_interceptor` does not call into `_init`, only the reverse: `init()` calls `install()` and `dispose()` calls `uninstall()`. Holding the init lock while transitively acquiring the interceptor lock is safe — they're nested, never inverted. `RLock` (not `Lock`) is used in both places so the same thread can re-enter from a wrapping caller without deadlocking.

**Tech Stack:** Python ≥ 3.9 stdlib (`threading.RLock`, `sys.setswitchinterval` for deterministic race reproduction), pytest. No new dependencies.

**Pre-requisite:** Issue #1 (aggregator thread-safety, PR #25) is merged. The Aggregator RLock pattern is the same shape; reuse the same `sys.setswitchinterval` discipline in tests.

---

## File Structure

| File | Role |
|---|---|
| `recost/_interceptor.py` | Add module-level `_install_lock = threading.RLock()`; wrap bodies of `install()`, `uninstall()`, `is_installed()`. |
| `recost/_init.py` | Add module-level `_init_lock = threading.RLock()`; wrap bodies of `init()` and `RecostHandle.dispose()` plus the `_handle` global mutation in `dispose()`. |
| `tests/test_interceptor.py` | Append a `TestInstallUninstallRace` class with two regression tests. |
| `tests/test_init.py` | Append a `TestInitDisposeRace` class with two regression tests. |

No public-API signature changes. No new files. No changes to `_aggregator.py`, `_transport.py`, `_provider_registry.py`, or any framework adapter.

---

## Task 1: Lock the interceptor (`install` / `uninstall` / `is_installed`)

**Files:**
- Modify: `recost/_interceptor.py`
- Test: `tests/test_interceptor.py`

### Step 1: Append the failing regression test

Append to `tests/test_interceptor.py` (at the bottom, after any existing test classes):

```python
# ---------------------------------------------------------------------------
# Thread safety — install/uninstall race
# ---------------------------------------------------------------------------

import sys
import threading


class TestInstallUninstallRace:
    """Regression tests for issue #4 — install/uninstall must be safe under
    concurrent calls so the patch state cannot drift away from the
    ``_installed`` flag."""

    def test_concurrent_install_uninstall_leaves_consistent_state(self):
        """N threads each loop install/uninstall many times. After all
        threads finish, the interceptor must be uninstalled and no exception
        should have been raised."""
        from recost._interceptor import install, uninstall, is_installed

        iterations_per_thread = 200
        num_threads = 4
        exceptions: list[BaseException] = []

        def _noop(_event) -> None:
            pass

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    install(_noop)
                    uninstall()
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        # Force aggressive GIL yields so the race fires reliably on modern
        # CPython. Restored in finally.
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
        assert not is_installed(), "interceptor should be uninstalled after the race"

    def test_concurrent_install_uninstall_does_not_leak_patches(self):
        """Pre-fix: two threads can both enter ``install()`` before either has
        set ``_installed = True``, both patch the target, and the second
        wraps the first. A subsequent ``uninstall()`` unwraps only the
        outer layer — the inner wrapper persists even after ``_installed``
        is False. This test verifies the original urllib3 callable is
        restored after a concurrent install/uninstall race."""
        try:
            import urllib3
        except ImportError:
            import pytest

            pytest.skip("urllib3 not installed")

        from recost._interceptor import install, uninstall, is_installed

        original_urlopen = urllib3.HTTPConnectionPool.urlopen

        def _noop(_event) -> None:
            pass

        iterations_per_thread = 200
        num_threads = 4
        exceptions: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    install(_noop)
                    uninstall()
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
        assert not is_installed(), "interceptor should be uninstalled"
        # The critical invariant: urllib3's method is back to its real
        # original, not a recost wrapper.
        assert urllib3.HTTPConnectionPool.urlopen is original_urlopen, (
            "concurrent install/uninstall left a recost wrapper in place"
        )
```

### Step 2: Run the test and confirm it FAILS

```
python -m pytest tests/test_interceptor.py::TestInstallUninstallRace -v
```

Expected:
- `test_concurrent_install_uninstall_leaves_consistent_state` may pass intermittently — the failure mode is subtle.
- `test_concurrent_install_uninstall_does_not_leak_patches` should reliably FAIL with `concurrent install/uninstall left a recost wrapper in place` (proving the double-patch leak).

If the second test passes on the first run (rare), bump `iterations_per_thread` to 500 and re-run. It must reliably fail before proceeding.

### Step 3: Do NOT commit yet — bundled with the fix below.

### Step 4: Add `_install_lock` and wrap the public API

Edit `recost/_interceptor.py`. Find the module-level state block near the top:

```python
import contextvars
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

from ._types import RawEvent
```

Replace with:

```python
import contextvars
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

from ._types import RawEvent
```

Then find the module-level singleton state block:

```python
# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

_installed: bool = False
_callback: Optional[EventCallback] = None
```

Replace with:

```python
# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

_installed: bool = False
_callback: Optional[EventCallback] = None

# Guards install / uninstall / is_installed so the patched-method state
# cannot drift away from the _installed flag under concurrent callers.
# RLock so the same thread can re-enter (e.g. from a callback that
# triggers reinstall). See issue #4.
_install_lock: threading.RLock = threading.RLock()
```

### Step 5: Wrap `install`, `uninstall`, `is_installed`

Find the public API block at the bottom of `_interceptor.py`:

```python
def install(callback: EventCallback) -> None:
    """Install patches on urllib3, httpx, and aiohttp. No-op if already installed."""
    global _installed, _callback
    if _installed:
        return

    _callback = callback
    _patch_urllib3()
    _patch_httpx()
    _patch_aiohttp()
    _installed = True


def uninstall() -> None:
    """Restore all patched functions to their originals. No-op if not installed."""
    global _installed, _callback
    if not _installed:
        return

    _unpatch_urllib3()
    _unpatch_httpx()
    _unpatch_aiohttp()
    _callback = None
    _installed = False


def is_installed() -> bool:
    """Returns True if patches are currently active."""
    return _installed
```

Replace with:

```python
def install(callback: EventCallback) -> None:
    """Install patches on urllib3, httpx, and aiohttp. No-op if already installed."""
    global _installed, _callback
    with _install_lock:
        if _installed:
            return

        _callback = callback
        _patch_urllib3()
        _patch_httpx()
        _patch_aiohttp()
        _installed = True


def uninstall() -> None:
    """Restore all patched functions to their originals. No-op if not installed."""
    global _installed, _callback
    with _install_lock:
        if not _installed:
            return

        _unpatch_urllib3()
        _unpatch_httpx()
        _unpatch_aiohttp()
        _callback = None
        _installed = False


def is_installed() -> bool:
    """Returns True if patches are currently active."""
    with _install_lock:
        return _installed
```

### Step 6: Run the race tests and confirm they pass

```
python -m pytest tests/test_interceptor.py::TestInstallUninstallRace -v
```

Expected: BOTH tests PASS within ~10s combined.

### Step 7: Run the full interceptor test suite to confirm no regressions

```
python -m pytest tests/test_interceptor.py -v
```

Expected: every existing test still passes.

### Step 8: Commit test + fix together

```
git add recost/_interceptor.py tests/test_interceptor.py
git commit -m "$(cat <<'EOF'
fix(interceptor): guard install/uninstall with RLock

Two threads racing install() could both observe _installed=False and
both run the patch path, with the second wrapping the first. A later
uninstall() would unwrap only one layer, leaving a recost wrapper in
place even though _installed=False.

Add a module-level RLock around install/uninstall/is_installed so the
patched-method state cannot drift away from the _installed flag.

Adds a regression test that reliably reproduced the wrapper-leak on
the unfixed code.

Refs #4
EOF
)"
```

---

## Task 2: Lock `init` and `dispose`

**Files:**
- Modify: `recost/_init.py`
- Test: `tests/test_init.py`

### Step 1: Append the failing regression test

Append to `tests/test_init.py` (at the bottom):

```python
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
        from recost._interceptor import is_installed

        # Disabled config avoids actually starting transport / timer threads
        # so the test stays fast and isolated. The lock invariant we are
        # testing does not depend on those resources being live.
        config_factory = lambda: RecostConfig(enabled=True)
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
        assert is_installed(), "interceptor must be installed for the active handle"

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
                    handle = init(RecostConfig(enabled=True))
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
```

### Step 2: Run the tests and confirm they fail

```
python -m pytest tests/test_init.py::TestInitDisposeRace -v
```

Expected: `test_concurrent_init_does_not_orphan_handles` reliably FAILS with `expected exactly 1 undisposed handle, got N` (where N > 1) — proves the race orphans handles.

The second test (`test_concurrent_init_dispose_leaves_module_clean`) may pass intermittently — it's the weaker invariant.

If neither fails on the first run, bump `iterations_per_thread` to 50 and re-run. The first one must reliably fail before proceeding.

### Step 3: Do NOT commit yet — bundled with the fix below.

### Step 4: Add `_init_lock` to `_init.py`

Edit `recost/_init.py`. Find the module-level `_handle` declaration:

```python
# Module-level handle so a second init() call disposes the first.
_handle: Optional[RecostHandle] = None
```

Replace with:

```python
# Module-level handle so a second init() call disposes the first.
_handle: Optional[RecostHandle] = None

# Guards init() and dispose() so the _handle global cannot become
# inconsistent under concurrent callers. RLock so a wrapping caller
# (e.g. dispose() running on the timer thread which itself owns the
# init lock) does not deadlock. See issue #4.
_init_lock: threading.RLock = threading.RLock()
```

### Step 5: Wrap `init()` body with `_init_lock`

Find the `init()` function. Its full body (after the module-level `_handle` declaration) is currently:

```python
def init(config: Optional[RecostConfig] = None) -> RecostHandle:
    """
    Initialize the ReCost SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    if _handle is not None:
        _handle.dispose()

    config = config or RecostConfig()
    ...
```

Wrap the entire body in `with _init_lock:`. Find this exact opening:

```python
def init(config: Optional[RecostConfig] = None) -> RecostHandle:
    """
    Initialize the ReCost SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    if _handle is not None:
        _handle.dispose()
```

Replace with:

```python
def init(config: Optional[RecostConfig] = None) -> RecostHandle:
    """
    Initialize the ReCost SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    with _init_lock:
        if _handle is not None:
            _handle.dispose()
```

The closing `return handle` at the end of `init()` must also be inside the `with` block. Indent every line from `if _handle is not None: _handle.dispose()` through `return handle` by one additional level (4 spaces). The `with _init_lock:` block ends at the `return handle` line; the function ends immediately after.

**Verification after edit:** open `recost/_init.py` and confirm that the line `return handle` (around line 226) is indented one level deeper than `def init`. Run `python -c "import recost._init"` to confirm no IndentationError.

### Step 6: Wrap `RecostHandle.dispose()` body with `_init_lock`

In `recost/_init.py`, find `RecostHandle.dispose()`:

```python
    def dispose(self) -> None:
        """Stop intercepting, flush remaining events, and close transport connections.

        Stops the periodic timer first so no new flush can race the shutdown
        flush, then runs one final flush in a worker thread bounded by
        ``shutdown_flush_timeout_ms``. The transport is only disposed after
        the final flush settles or its timeout elapses, so an in-flight
        cloud POST is not cut off mid-request.
        """
        if self._disposed:
            return
        self._disposed = True

        self._timer_stop.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=5.0)

        if self._final_flush is not None:
            flush_thread = threading.Thread(target=self._final_flush, daemon=True)
            flush_thread.start()
            flush_thread.join(timeout=self._shutdown_flush_timeout_ms / 1000.0)

        uninstall()

        if self._transport is not None:
            self._transport.dispose()

        global _handle
        if _handle is self:
            _handle = None
```

Wrap the body with `_init_lock`. Replace with:

```python
    def dispose(self) -> None:
        """Stop intercepting, flush remaining events, and close transport connections.

        Stops the periodic timer first so no new flush can race the shutdown
        flush, then runs one final flush in a worker thread bounded by
        ``shutdown_flush_timeout_ms``. The transport is only disposed after
        the final flush settles or its timeout elapses, so an in-flight
        cloud POST is not cut off mid-request.
        """
        with _init_lock:
            if self._disposed:
                return
            self._disposed = True

            self._timer_stop.set()
            if self._timer_thread is not None:
                self._timer_thread.join(timeout=5.0)

            if self._final_flush is not None:
                flush_thread = threading.Thread(target=self._final_flush, daemon=True)
                flush_thread.start()
                flush_thread.join(timeout=self._shutdown_flush_timeout_ms / 1000.0)

            uninstall()

            if self._transport is not None:
                self._transport.dispose()

            global _handle
            if _handle is self:
                _handle = None
```

**Note:** `dispose()` holding `_init_lock` while calling `uninstall()` (which acquires the interceptor's `_install_lock`) is safe — the locks are independent and always acquired in the same order (init lock first, install lock second). No inversion possible.

### Step 7: Run the race tests and confirm they pass

```
python -m pytest tests/test_init.py::TestInitDisposeRace -v
```

Expected: BOTH tests PASS within ~15s combined.

### Step 8: Run the full init test suite

```
python -m pytest tests/test_init.py -v
```

Expected: every existing test still passes.

### Step 9: Commit test + fix together

```
git add recost/_init.py tests/test_init.py
git commit -m "$(cat <<'EOF'
fix(init): guard init/dispose with RLock

Two threads racing init() could both observe _handle=None and both
proceed past the dispose-previous guard, with the first thread's
handle orphaned (its timer thread + transport leak) once the second
overwrites _handle.

Add a module-level RLock around init() and RecostHandle.dispose() so
_handle mutation and the install/uninstall calls they make stay
atomic against each other.

The lock nests safely with the interceptor's _install_lock — _init_lock
is always acquired first, then _install_lock via the transitive
install()/uninstall() call.

Adds a regression test that reliably reproduced the orphan-handle leak
on the unfixed code.

Refs #4
EOF
)"
```

---

## Task 3: Final verification + open PR

### Step 1: Run the full test suite

```
python -m pytest
```

Expected: every test passes (including the 4 new race tests across `test_interceptor.py` and `test_init.py`).

Some tests may emit `DeprecationWarning` from the `flush_interval` legacy alias — that's expected and unrelated.

### Step 2: Ruff on touched files

```
python -m ruff check recost/_interceptor.py recost/_init.py tests/test_interceptor.py tests/test_init.py
```

Expected: no errors introduced by this PR. Pre-existing `F401` for `MAX_BUCKETS` in `_transport.py` is tracked by issue #13 and out of scope.

### Step 3: Mypy on the changed files

```
python -m mypy recost/_interceptor.py recost/_init.py
```

Expected: no NEW errors compared to main. The repo overall has ~33 known mypy errors tracked by issue #2; this PR must not increase that count for the two files it touches.

To verify, run mypy on `main` first, capture the error count for these two files, then compare with the branch. If the count is equal or lower for both files, the PR passes.

### Step 4: Push and open the PR

```
git push -u origin fix/module-level-state-races
gh pr create --title "fix: guard module-level state with RLock (closes #4)" --body "$(cat <<'EOF'
## Summary

Closes #4.

Adds two module-level `RLock`s to guard the singleton state shared
across SDK lifecycle calls:

- `_install_lock` in `recost/_interceptor.py` — protects
  `install()` / `uninstall()` / `is_installed()` so the patched-method
  state cannot drift away from the `_installed` flag.
- `_init_lock` in `recost/_init.py` — protects `init()` and
  `RecostHandle.dispose()` so concurrent callers cannot orphan
  previously-installed handles.

The two locks are independent. `init()` / `dispose()` always acquire
`_init_lock` first, then `_install_lock` via the transitive
`install()` / `uninstall()` call. There is no inverted ordering, so
no deadlock potential.

## Tests

- `tests/test_interceptor.py::TestInstallUninstallRace` — 4 threads ×
  200 install/uninstall cycles, with `sys.setswitchinterval(0.000001)`
  to force GIL yields. Pre-fix this reliably leaves a recost wrapper
  on `urllib3.HTTPConnectionPool.urlopen` after the race; post-fix
  the original is restored.
- `tests/test_init.py::TestInitDisposeRace` — 4 threads × 20 init
  cycles. Pre-fix this reliably orphans handles (multiple handles
  returned from `init()` end up undisposed); post-fix exactly one
  handle is active at any time.

## Notes

- Public API unchanged.
- No new dependencies.
- Builds on the same `sys.setswitchinterval` test discipline introduced
  by PR #25 (issue #1).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Step 5: Verify the PR is open

```
gh pr view --json url,state,headRefName
```

Expected: `state: "OPEN"`, `headRefName: "fix/module-level-state-races"`.

### Step 6: Report back

Return one of: **DONE** / **DONE_WITH_CONCERNS** / **NEEDS_CONTEXT** / **BLOCKED**. Include:
- Test count and pass/fail
- Ruff result on the four files
- Mypy result on the two source files
- PR URL

Under 150 words.

---

## Self-review

- **Spec coverage:** Issue #4 requires (1) RLock around `init()` / `dispose()` / `install()` / `uninstall()` — done in Tasks 1 and 2; (2) tests that race both pairs — done in Tasks 1 and 2; (3) the two locks nest safely without inversion — documented in Task 2 Step 6 note.
- **Placeholder scan:** No TBDs. Every code step shows the exact Find/Replace. Every test has a full body. Every git command has the literal title/body.
- **Type consistency:** `threading.RLock()` is used in both modules; `_install_lock` and `_init_lock` are the only new symbols. No public-API additions. `EventCallback`, `RecostConfig`, `RecostHandle` are all referenced consistently with their existing definitions.
- **Dependencies:** Requires PR #25 (issue #1, aggregator thread-safety) to be merged. This plan creates a fresh worktree off `main` post-merge.
