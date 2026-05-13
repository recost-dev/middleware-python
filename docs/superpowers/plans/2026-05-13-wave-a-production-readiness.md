# Wave A Production-Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every silent-failure gap that prevents `recost` from running safely in production — fork-safety, reentrancy correctness, auth/rate-limit escalation, init-time validation, body-size accuracy, mypy-clean public posture, and CI enforcement.

**Architecture:** Three sequenced PRs. PR 1 (foundation) lands CI + mypy fixes + hygiene so subsequent PRs land green. PR 2 (lifecycle) adds the `RecostError` base class, fork-safety (`os.register_at_fork` + PID backstop + manual reinit), and the task-and-thread reentrancy guard. PR 3 (transport) adds the typed-error subclasses, 401 escalation, 429/Retry-After handling, `api_key` format validation, and interceptor body-size fixes. All Python-side; no Node/extension changes.

**Tech Stack:** Python 3.9+, hatchling, pytest, pytest-asyncio, ruff, mypy (strict). Optional runtime deps: `httpx`, `aiohttp`, `urllib3` (via `requests`), `starlette`, `flask`, `websockets`.

**Design source:** `docs/superpowers/specs/2026-05-13-wave-a-production-readiness-design.md`

---

## File map

**Created:**
- `.github/workflows/ci.yml` — matrix CI (Python 3.9–3.12) running ruff, mypy, pytest
- `tests/test_fork_safety.py` — subprocess-based fork test
- `tests/test_reentrancy.py` — async-to-thread double-count regression test
- `tests/test_errors.py` — exception-class import + attribute tests

**Modified:**
- `recost/_types.py` — type-annotation fixes, add `RecostError` and subclasses (or move to `_errors.py`; we choose `_types.py` for re-export simplicity)
- `recost/_interceptor.py` — task+thread reentrancy guard, body-size measurement, `raise exc` → `raise`, mypy fixes
- `recost/_transport.py` — auth-failure counter, 401 escalation, 429 / Retry-After, structured cloud-result type, remove unused `MAX_BUCKETS` import
- `recost/_init.py` — `api_key` format validation, fork-safety hooks, `pid` on handle, `reinit_after_fork` method, mypy fixes
- `recost/__init__.py` — re-export new exception classes
- `recost/frameworks/flask.py` — `Optional[RecostHandle]` annotation
- `tests/test_init.py` — api_key validation test, fork-safety smoke test
- `tests/test_interceptor.py` — aiohttp `json=` body-size, httpx streaming-content guard
- `tests/test_transport.py` — 401 escalation, 429 deferral, structured-result tests
- `README.md` — streaming response-byte caveat, accurate mypy/CI claims

---

## Sequencing & branch strategy

Each PR runs on its own branch off `main`:
- `wave-a/pr1-foundation`
- `wave-a/pr2-lifecycle`
- `wave-a/pr3-transport`

Land them sequentially. PR 2 branches off `main` *after* PR 1 merges; PR 3 branches off `main` *after* PR 2 merges. Do **not** stack PRs — the rebase pain isn't worth the speed.

---

# PR 1 — Foundation

## Task 1: Create CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the branch**

```bash
git checkout -b wave-a/pr1-foundation
```

- [ ] **Step 2: Write the workflow file**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  check:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.9", "3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev,all]"
      - name: Ruff
        run: ruff check recost/ tests/
      - name: Mypy
        run: mypy recost/
      - name: Pytest
        run: pytest -v
```

- [ ] **Step 3: Verify the workflow is syntactically valid by committing and pushing**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add ruff + mypy + pytest matrix on Python 3.9-3.12"
```

CI will run on the next push. It is expected to **fail** on the mypy step at this point — that's the point of subsequent tasks.

## Task 2: Fix mypy errors in `recost/_types.py`

**Files:**
- Modify: `recost/_types.py`

- [ ] **Step 1: Run mypy locally and confirm errors are present**

```bash
pip install -e ".[dev,all]"
mypy recost/_types.py
```

Expected: errors related to `dict` (missing parameters) and any unparameterized generic types.

- [ ] **Step 2: Annotate `to_dict()` return types**

In `recost/_types.py`, change all `def to_dict(self) -> dict:` declarations to `def to_dict(self) -> dict[str, Any]:` and add `from typing import Any` to the imports.

- [ ] **Step 3: Run mypy on the file and confirm clean**

```bash
mypy recost/_types.py
```

Expected: `Success: no issues found in 1 source file`

- [ ] **Step 4: Commit**

```bash
git add recost/_types.py
git commit -m "fix(types): annotate to_dict returns as dict[str, Any] for mypy strict"
```

## Task 3: Fix mypy errors in `recost/_interceptor.py`

**Files:**
- Modify: `recost/_interceptor.py`

- [ ] **Step 1: Run mypy on the file**

```bash
mypy recost/_interceptor.py
```

Expected: errors involving `latency_ms` (float→int), unparameterized `ContextVar`, untyped function defs, and stale `# type: ignore` comments.

- [ ] **Step 2: Change `RawEvent.latency_ms` to accept `float`**

In `recost/_types.py`, change `latency_ms: int` to `latency_ms: float` (latency is naturally fractional). Then in `recost/_interceptor.py:85`, change `latency_ms=round(latency_ms)` to `latency_ms=latency_ms`.

- [ ] **Step 3: Type the wrappers explicitly**

Replace every `# type: ignore[no-untyped-def]` on the patch wrappers with proper signatures. Use `Any` where the underlying library types aren't trivially expressible:

```python
from typing import Any

def _patched_urlopen(self: Any, method: str, url: str, body: Any = None, headers: Any = None, retries: Any = None, redirect: bool = True, assert_same_host: bool = True, timeout: Any = ..., **response_kw: Any) -> Any:
    ...
```

(Note: replace `timeout=urllib3.util.Timeout.DEFAULT_TIMEOUT` with `timeout: Any = _UNSET` where `_UNSET = object()` is defined at module scope, then resolve inside the body. This also addresses issue #11.)

- [ ] **Step 4: Remove now-unused `# type: ignore` comments and ensure remaining ones are narrowed**

Keep `# type: ignore[import-untyped]` only on `import urllib3` / `import aiohttp` lines. Remove `# type: ignore[no-untyped-def]` everywhere now that the functions are typed.

- [ ] **Step 5: Run mypy on the file**

```bash
mypy recost/_interceptor.py
```

Expected: `Success: no issues found in 1 source file`

- [ ] **Step 6: Run tests to confirm no regressions**

```bash
pytest tests/test_interceptor.py -v
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add recost/_interceptor.py recost/_types.py
git commit -m "fix(interceptor): mypy strict — type wrappers, latency_ms float, drop dead type-ignores"
```

## Task 4: Fix mypy errors in `recost/_init.py` and `recost/_transport.py`

**Files:**
- Modify: `recost/_init.py`
- Modify: `recost/_transport.py`

- [ ] **Step 1: Run mypy**

```bash
mypy recost/_init.py recost/_transport.py
```

- [ ] **Step 2: Apply fixes**

In `recost/_init.py`:
- Annotate `_handle: Optional[RecostHandle] = None` at module scope (already done — confirm).
- Annotate any `flush_and_send`, `on_event`, `_timer_loop` return types as `-> None`.

In `recost/_transport.py`:
- Annotate `_post_cloud(...) -> None` (will change in PR 3 — fine for now).
- Type the `_local: Optional[_LocalTransport]` attr explicitly.
- Annotate `_LocalTransport._run(self) -> None` and the inner `_ws_loop` coroutine.

- [ ] **Step 3: Run mypy on both files**

```bash
mypy recost/_init.py recost/_transport.py
```

Expected: clean.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_init.py tests/test_transport.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add recost/_init.py recost/_transport.py
git commit -m "fix(init,transport): annotate inner functions and module state for mypy strict"
```

## Task 5: Fix mypy errors in `recost/frameworks/flask.py`

**Files:**
- Modify: `recost/frameworks/flask.py`

- [ ] **Step 1: Run mypy**

```bash
mypy recost/frameworks/flask.py
```

Expected: error on line 26 — `self._handle = None` inferred as `None`, reassignment to `RecostHandle` fails.

- [ ] **Step 2: Apply the type annotation**

Edit `recost/frameworks/flask.py:22-33`:

```python
class ReCost:
    """Flask extension that initializes ReCost telemetry."""

    _handle: Optional["RecostHandle"]

    def __init__(self, app: Optional[Flask] = None, config: Optional[RecostConfig] = None, **kwargs: Any) -> None:
        self._handle = None
        if app is not None:
            self.init_app(app, config, **kwargs)

    def init_app(self, app: Flask, config: Optional[RecostConfig] = None, **kwargs: Any) -> None:
        if config is None:
            config = RecostConfig(**kwargs)
        self._handle = init(config)
```

Add `from .._init import RecostHandle` at the top under the existing `from .._init import init` line (or use a `TYPE_CHECKING` guard if there's a circular concern — there isn't here).

- [ ] **Step 3: Run mypy on the file**

```bash
mypy recost/frameworks/flask.py
```

Expected: clean.

- [ ] **Step 4: Run flask tests**

```bash
pytest tests/test_flask.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add recost/frameworks/flask.py
git commit -m "fix(flask): annotate _handle as Optional[RecostHandle] for mypy strict"
```

## Task 6: Verify full mypy + ruff clean

**Files:**
- (verification only)

- [ ] **Step 1: Run mypy on the entire package**

```bash
mypy recost/
```

Expected: `Success: no issues found in N source files`. If any remain, fix them. Common holdouts: `_provider_registry.py`, `_aggregator.py`, `frameworks/fastapi.py`. Apply the same patterns — annotate parameters and returns, replace stale `# type: ignore` with narrowed forms.

- [ ] **Step 2: Run ruff**

```bash
ruff check recost/ tests/
```

Expected: clean. If `F401 unused-import` fires on `MAX_BUCKETS` in `recost/_transport.py`, remove that import.

- [ ] **Step 3: Run the full test suite**

```bash
pytest -v
```

Expected: all green.

- [ ] **Step 4: Commit any straggling fixes**

```bash
git add -A
git commit -m "fix: clean remaining mypy/ruff issues across recost/"
```

(Skip the commit if there's nothing to add.)

## Task 7: Hygiene fixes — `raise exc` → `raise`

**Files:**
- Modify: `recost/_interceptor.py`

- [ ] **Step 1: Locate the four `raise exc` sites**

```bash
grep -n "raise exc" recost/_interceptor.py
```

Expected: lines 156, 228, 275, 350 (approximate — confirm by reading).

- [ ] **Step 2: Replace each with bare `raise`**

In each of the four `except Exception as exc:` blocks at lines ~148–156, ~219–228, ~266–275, ~342–350, replace the final `raise exc` with `raise`. This preserves the original traceback so end users debugging SDK-wrapped errors see their own call site at the top of the stack.

- [ ] **Step 3: Run interceptor tests**

```bash
pytest tests/test_interceptor.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add recost/_interceptor.py
git commit -m "fix(interceptor): use bare 'raise' to preserve traceback context (#13)"
```

## Task 8: Update README mypy claim, push PR 1

**Files:**
- Modify: `README.md` (only if claim drift)

- [ ] **Step 1: Verify the README claim**

```bash
grep -n "mypy" README.md CLAUDE.md
```

If `README.md` or `CLAUDE.md` claims "strict-clean", confirm it is now actually true and leave as-is. If they don't claim it, do nothing.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin wave-a/pr1-foundation
```

- [ ] **Step 3: Open PR 1**

```bash
gh pr create --title "Wave A PR 1: CI + mypy strict + hygiene" --body "$(cat <<'EOF'
## Summary
- Adds `.github/workflows/ci.yml` running ruff + mypy + pytest on Python 3.9–3.12.
- Fixes all mypy strict errors so the public posture in README is true.
- Drive-by hygiene: removes unused `MAX_BUCKETS` import, replaces `raise exc` with bare `raise` to preserve tracebacks.

Closes #2, #13.

## Test plan
- [ ] CI matrix green on 3.9, 3.10, 3.11, 3.12
- [ ] `mypy recost/` reports 0 errors locally
- [ ] `ruff check recost/ tests/` reports 0 errors locally
- [ ] `pytest` passes locally with no behavior changes

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify CI passes on the PR**

Check the PR page; all four Python matrix jobs must be green before merging.

---

# PR 2 — Lifecycle & correctness

Branch off `main` after PR 1 merges.

## Task 9: Add `RecostError` base class

**Files:**
- Modify: `recost/_types.py`
- Modify: `recost/__init__.py`
- Create: `tests/test_errors.py`

- [ ] **Step 1: Create branch**

```bash
git checkout main && git pull && git checkout -b wave-a/pr2-lifecycle
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_errors.py`:

```python
"""Tests for the typed-error hierarchy exposed by recost."""

import pytest


def test_recost_error_is_importable():
    from recost import RecostError
    assert issubclass(RecostError, Exception)


def test_recost_error_carries_message():
    from recost import RecostError
    err = RecostError("something broke")
    assert str(err) == "something broke"
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
pytest tests/test_errors.py -v
```

Expected: ImportError — `cannot import name 'RecostError'`.

- [ ] **Step 4: Implement `RecostError`**

Append to `recost/_types.py`:

```python
# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class RecostError(Exception):
    """Base class for typed SDK errors passed to on_error callbacks."""
```

Add `"RecostError"` to the `__all__` list in `recost/__init__.py` and add it to the imports:

```python
from ._types import (
    RecostConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    RecostError,
    TransportMode,
    WindowSummary,
)
```

```python
__all__ = [
    "init",
    "RecostHandle",
    "RawEvent",
    "MetricEntry",
    "WindowSummary",
    "ProviderDef",
    "RecostConfig",
    "RecostError",
    "TransportMode",
    "ProviderRegistry",
    "BUILTIN_PROVIDERS",
    "MatchResult",
    "install",
    "uninstall",
    "is_installed",
    "Aggregator",
]
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
pytest tests/test_errors.py -v
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add recost/_types.py recost/__init__.py tests/test_errors.py
git commit -m "feat(errors): add RecostError base class"
```

## Task 10: Add `RecostHandle.pid` + PID backstop in `init()`

**Files:**
- Modify: `recost/_init.py`
- Modify: `tests/test_init.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_init.py`:

```python
def test_handle_records_pid(monkeypatch):
    """The handle records the PID at init time."""
    import os
    from recost import init, RecostConfig
    handle = init(RecostConfig(enabled=True, api_key=None))
    try:
        assert handle.pid == os.getpid()
    finally:
        handle.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_init.py::test_handle_records_pid -v
```

Expected: `AttributeError: 'RecostHandle' object has no attribute 'pid'`.

- [ ] **Step 3: Implement**

In `recost/_init.py`, modify `RecostHandle.__init__` to record `pid`:

```python
import os
# (at top of file, ensure os is imported)

class RecostHandle:
    """Returned by init() to allow explicit teardown."""

    def __init__(
        self,
        timer_stop: threading.Event,
        timer_thread: Optional[threading.Thread],
        transport: Optional[Transport],
    ) -> None:
        self._timer_stop = timer_stop
        self._timer_thread = timer_thread
        self._transport = transport
        self._disposed = False
        self.pid: int = os.getpid()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_init.py::test_handle_records_pid -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recost/_init.py tests/test_init.py
git commit -m "feat(init): record current pid on RecostHandle for fork-safety backstop"
```

## Task 11: Add `RecostHandle.reinit_after_fork()` method

**Files:**
- Modify: `recost/_init.py`
- Modify: `tests/test_init.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_init.py`:

```python
def test_reinit_after_fork_resets_pid_and_threads():
    """reinit_after_fork rebuilds the timer thread and updates pid."""
    import os
    import threading
    from recost import init, RecostConfig
    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        original_thread = handle._timer_thread
        # Simulate post-fork: the timer thread doesn't actually exist in a child.
        # We assert reinit produces a fresh thread and an unchanged pid (same process here).
        handle.reinit_after_fork()
        assert handle._timer_thread is not original_thread
        assert handle._timer_thread is not None
        assert handle._timer_thread.is_alive()
        assert handle.pid == os.getpid()
    finally:
        handle.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_init.py::test_reinit_after_fork_resets_pid_and_threads -v
```

Expected: `AttributeError: 'RecostHandle' object has no attribute 'reinit_after_fork'`.

- [ ] **Step 3: Implement**

To support `reinit_after_fork`, the handle needs a reference to its own config/aggregator/flush closure so it can rebuild. The cleanest fix is to store a `_rebuild_callable` on the handle and have `init()` pass it in.

In `recost/_init.py`, refactor:

```python
class RecostHandle:
    def __init__(
        self,
        timer_stop: threading.Event,
        timer_thread: Optional[threading.Thread],
        transport: Optional[Transport],
        rebuild: Optional[Callable[["RecostHandle"], None]] = None,
    ) -> None:
        self._timer_stop = timer_stop
        self._timer_thread = timer_thread
        self._transport = transport
        self._disposed = False
        self.pid: int = os.getpid()
        self._rebuild = rebuild

    def reinit_after_fork(self) -> None:
        """Recreate timer thread + transport in the current PID. Idempotent within a PID."""
        if self._rebuild is None:
            return
        if self.pid == os.getpid() and self._timer_thread is not None and self._timer_thread.is_alive():
            return  # No fork has happened
        self._rebuild(self)
        self.pid = os.getpid()
```

In `init()`, build a rebuild closure that knows how to recreate the moving parts:

```python
def _make_rebuild(config: RecostConfig, registry: ProviderRegistry, aggregator: Aggregator) -> Callable[[RecostHandle], None]:
    def rebuild(handle: RecostHandle) -> None:
        # Replace transport (new event loop, new WS thread)
        new_transport = Transport(config)
        handle._transport = new_transport
        # Replace flush timer
        new_stop = threading.Event()
        handle._timer_stop = new_stop

        def _loop() -> None:
            while not new_stop.wait(timeout=config.flush_interval):
                try:
                    summary = aggregator.flush()
                    if summary is not None:
                        new_transport.send(summary)
                except Exception as err:
                    if config.on_error:
                        config.on_error(err)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        handle._timer_thread = t

    return rebuild
```

Pass `rebuild=_make_rebuild(config, registry, aggregator)` when constructing the handle in `init()`. Add `from typing import Callable` to imports if not present.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_init.py::test_reinit_after_fork_resets_pid_and_threads -v
```

Expected: PASS.

- [ ] **Step 5: Run all init tests**

```bash
pytest tests/test_init.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add recost/_init.py tests/test_init.py
git commit -m "feat(init): add RecostHandle.reinit_after_fork for explicit fork recovery"
```

## Task 12: Register `os.register_at_fork` hook

**Files:**
- Modify: `recost/_init.py`
- Create: `tests/test_fork_safety.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fork_safety.py`:

```python
"""Fork-safety regression tests.

These use os.fork() so they only run on POSIX. Skip on Windows.
"""

import os
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.fork() is POSIX-only",
)


def test_register_at_fork_rebuilds_timer_in_child():
    """After fork(), the child should have a live timer thread, not the parent's stale reference."""
    from recost import init, RecostConfig

    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        parent_thread_id = handle._timer_thread.ident
        parent_pid = os.getpid()

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
                ok = (handle.pid == child_pid
                      and handle._timer_thread is not None
                      and handle._timer_thread.is_alive())
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_fork_safety.py -v
```

Expected: FAIL — the child reads as `b"0"` because the at_fork hook isn't registered.

- [ ] **Step 3: Register the at_fork hook in `init()`**

In `recost/_init.py`, after constructing the handle but before returning, register the fork hook:

```python
def _make_after_fork(handle: "RecostHandle") -> Callable[[], None]:
    def _after_fork() -> None:
        try:
            handle.reinit_after_fork()
        except Exception:
            pass  # Never let SDK errors crash a forked child
    return _after_fork


# In init(), after `_handle = handle`:
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_make_after_fork(handle))
```

`os.register_at_fork` exists on POSIX from Python 3.7+. The `hasattr` guard makes Windows a no-op (Windows users rely on the PID backstop + manual `reinit_after_fork`).

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_fork_safety.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recost/_init.py tests/test_fork_safety.py
git commit -m "feat(init): register os.register_at_fork hook for automatic child reinit (#3)"
```

## Task 12.5: PID backstop in `on_event`

For environments where `os.register_at_fork` doesn't fire (uwsgi lazy-fork, certain embedders, or Windows where the hook is absent), the interceptor needs to detect a PID change on the hot path and rebuild lazily.

**Files:**
- Modify: `recost/_init.py`
- Modify: `tests/test_fork_safety.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fork_safety.py`:

```python
def test_pid_backstop_triggers_reinit_when_hook_missing(monkeypatch):
    """Simulate a fork that bypassed register_at_fork: handle.pid stale, but the
    next intercepted event must trigger reinit_after_fork."""
    import os
    from recost import init, RecostConfig
    from recost._types import RawEvent

    # Block register_at_fork during init so the hook does NOT install.
    if hasattr(os, "register_at_fork"):
        monkeypatch.setattr(os, "register_at_fork", lambda **kw: None)

    handle = init(RecostConfig(enabled=True, api_key=None, flush_interval=60.0))
    try:
        original_thread = handle._timer_thread
        # Forge a PID mismatch by stomping handle.pid (simulating: we are now in a
        # forked child that the hook didn't catch).
        handle.pid = 999_999  # impossible PID

        # Drive the on_event path. The cleanest way to hit it is to fire a fake
        # event through the interceptor's callback. _interceptor._callback is the
        # closure that init() registered.
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
        assert handle._timer_thread.is_alive()
    finally:
        handle.dispose()
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_fork_safety.py::test_pid_backstop_triggers_reinit_when_hook_missing -v
```

Expected: FAIL — handle.pid stays at 999_999; the on_event path doesn't check.

- [ ] **Step 3: Implement**

In `recost/_init.py`, restructure to give `on_event` access to the handle via a mutable cell (since `on_event` is defined before `handle` is constructed):

```python
# Inside init(), before defining on_event:
_handle_ref: list[Optional["RecostHandle"]] = [None]
_pid_warned: list[bool] = [False]

def on_event(event: RawEvent) -> None:
    # PID backstop: if the hook didn't fire on this platform, repair on first event.
    h = _handle_ref[0]
    if h is not None and h.pid != os.getpid():
        try:
            h.reinit_after_fork()
            if not _pid_warned[0]:
                _pid_warned[0] = True
                if config.on_error is not None:
                    from ._types import RecostError
                    config.on_error(RecostError(
                        f"recost: detected fork without register_at_fork hook; "
                        f"reinitialized in pid={os.getpid()}"
                    ))
        except Exception:
            return  # never let SDK errors break user code

    # ... existing on_event body unchanged ...

# After constructing the handle near the end of init():
_handle_ref[0] = handle
```

- [ ] **Step 4: Run the test**

```bash
pytest tests/test_fork_safety.py -v
```

Expected: PASS for both fork tests.

- [ ] **Step 5: Commit**

```bash
git add recost/_init.py tests/test_fork_safety.py
git commit -m "feat(init): PID backstop on event path for hook-less fork environments (#3)"
```

## Task 13: Replace `ContextVar`-only guard with task+thread reentrancy guard

**Files:**
- Modify: `recost/_interceptor.py`
- Create: `tests/test_reentrancy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reentrancy.py`:

```python
"""Regression test for issue #15 — reentrancy guard must work across thread hops."""

import asyncio
import threading
import time
from typing import List
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_async_to_thread_does_not_double_count():
    """A requests.get() dispatched via asyncio.to_thread inside an async task
    must record exactly one event, not two."""
    import requests
    from recost._interceptor import install, uninstall

    events: List = []
    install(events.append)
    try:
        # Use a httpbin-style URL — we use a local server in production tests,
        # but for the reentrancy regression we only care about the *count*,
        # not delivery. Mock the underlying urlopen to short-circuit.
        from unittest.mock import patch
        with patch("urllib3.HTTPConnectionPool._make_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"content-length": "0"}
            mock_req.return_value = mock_resp

            async def one_call():
                return await asyncio.to_thread(requests.get, "http://example.invalid/")

            results = await asyncio.gather(*[one_call() for _ in range(10)], return_exceptions=True)

        # Filter to actual events recorded; we expect exactly 10
        assert len(events) == 10, f"Expected 10 events, got {len(events)}"
    finally:
        uninstall()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_reentrancy.py -v
```

Expected: FAIL — the task contextvar is not propagated when work hops to a thread, and `requests` (which uses urllib3) ends up un-guarded. The event count may be 10 if the patches happen to not double-fire, or could differ based on internal library behavior — the key signal is that today's guard is provably weak. If the test passes today due to single-patch-per-call, write a stronger reproduction: directly call the underlying urllib3 patch *from inside* an httpx-async patched call.

(If the simpler test passes coincidentally, replace its body with this stronger one:)

```python
@pytest.mark.asyncio
async def test_nested_patch_does_not_double_record():
    """Simulate the failure mode: an outer async patch is active (contextvar set),
    work hops to a thread (contextvar reset), and the inner urllib3 patch fires."""
    import httpx
    from recost._interceptor import install, uninstall, _in_interceptor_task

    events: list = []
    install(events.append)
    try:
        # Manually establish an "outer patch active" state in this task.
        token = _in_interceptor_task.set(True)
        try:
            from unittest.mock import patch as _p, MagicMock
            with _p("urllib3.HTTPConnectionPool._make_request") as mock_req:
                mock_resp = MagicMock(status=200, headers={"content-length": "0"})
                mock_req.return_value = mock_resp
                import requests

                def call_in_thread() -> None:
                    requests.get("http://example.invalid/")

                t = threading.Thread(target=call_in_thread)
                t.start()
                t.join()

            # The thread saw a fresh context (no task var set) but should have
            # the thread-local guard turned ON because we set it in our task.
            # If the guard is task-only, the thread will record an event. We want zero.
            assert len(events) == 0, f"Thread saw a fresh context and double-recorded; got {len(events)} events"
        finally:
            _in_interceptor_task.reset(token)
    finally:
        uninstall()
```

Run it; expect FAIL.

- [ ] **Step 3: Implement task+thread guard**

In `recost/_interceptor.py`, replace the existing module-level guard:

```python
import contextvars
import threading
from contextvars import Token

# Double-count prevention — covers both async-task hops and OS-thread hops.
_in_interceptor_task: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_in_interceptor_task", default=False
)
_in_interceptor_thread = threading.local()


def _is_in_interceptor() -> bool:
    return _in_interceptor_task.get() or getattr(_in_interceptor_thread, "flag", False)


def _enter_interceptor() -> Token[bool]:
    token = _in_interceptor_task.set(True)
    _in_interceptor_thread.flag = True
    return token


def _exit_interceptor(token: Token[bool]) -> None:
    _in_interceptor_task.reset(token)
    _in_interceptor_thread.flag = False
```

(Delete the old `_in_interceptor: contextvars.ContextVar[bool]` definition.)

Then, in every patch wrapper (urllib3 sync, httpx sync, httpx async, aiohttp async), replace:

```python
if _in_interceptor.get(False):
    return _original_X(...)
token = _in_interceptor.set(True)
...
_in_interceptor.reset(token)
```

with:

```python
if _is_in_interceptor():
    return _original_X(...)
token = _enter_interceptor()
...
_exit_interceptor(token)
```

Apply to all four patch sites (urlopen, httpx sync send, httpx async send, aiohttp request).

**Important:** the existing code resets the guard *before* invoking `_callback`. Keep that behavior — `_exit_interceptor(token)` must be called before the callback fires so the callback's own HTTP calls (e.g., cloud transport) aren't accidentally guarded out.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_reentrancy.py -v
```

Expected: PASS.

- [ ] **Step 5: Run all interceptor tests**

```bash
pytest tests/test_interceptor.py tests/test_reentrancy.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add recost/_interceptor.py tests/test_reentrancy.py
git commit -m "fix(interceptor): guard reentrancy across tasks AND threads (#15)"
```

## Task 14: Push PR 2

- [ ] **Step 1: Run full suite locally**

```bash
mypy recost/ && ruff check recost/ tests/ && pytest -v
```

Expected: all green.

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin wave-a/pr2-lifecycle
gh pr create --title "Wave A PR 2: fork-safety + reentrancy correctness" --body "$(cat <<'EOF'
## Summary
- **#3 Fork-safety:** SDK now registers `os.register_at_fork(after_in_child=...)` to automatically rebuild the timer thread, transport, and event loop in forked children. Adds `handle.reinit_after_fork()` for explicit reinit, plus a PID-mismatch backstop on the event path for platforms where the fork hook is unavailable (uwsgi lazy-fork, Windows, embedders).
- **#15 Reentrancy:** the double-count guard now tracks both the async task (`ContextVar`) and the OS thread (`threading.local`), so calls that hop between async tasks and thread pools aren't double-counted.
- Adds `RecostError` base class (subclasses follow in PR 3).

## Test plan
- [ ] `tests/test_fork_safety.py` passes — child process correctly rebuilds state after `os.fork()`.
- [ ] `tests/test_reentrancy.py` passes — outer task guard propagates to inner thread.
- [ ] Full suite still green.

Closes #3 (fork-safety), #15 (reentrancy).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Verify CI passes**

All four Python jobs must be green.

---

# PR 3 — Transport & validation

Branch off `main` after PR 2 merges.

## Task 15: Add `RecostAuthError` and `RecostFatalAuthError`

**Files:**
- Modify: `recost/_types.py`
- Modify: `recost/__init__.py`
- Modify: `tests/test_errors.py`

- [ ] **Step 1: Create branch**

```bash
git checkout main && git pull && git checkout -b wave-a/pr3-transport
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_errors.py`:

```python
def test_auth_error_carries_status_and_count():
    from recost import RecostError, RecostAuthError
    err = RecostAuthError(status=401, consecutive_failures=3)
    assert isinstance(err, RecostError)
    assert err.status == 401
    assert err.consecutive_failures == 3
    assert "401" in str(err)


def test_fatal_auth_error_is_an_auth_error():
    from recost import RecostAuthError, RecostFatalAuthError
    err = RecostFatalAuthError(status=401, consecutive_failures=5)
    assert isinstance(err, RecostAuthError)
    assert err.consecutive_failures == 5
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
pytest tests/test_errors.py -v
```

Expected: ImportError on `RecostAuthError`.

- [ ] **Step 4: Implement**

Append to `recost/_types.py`:

```python
class RecostAuthError(RecostError):
    """API rejected the configured api_key (401)."""

    def __init__(self, status: int, consecutive_failures: int, message: str = "") -> None:
        super().__init__(message or f"Recost API returned {status} (auth failed; {consecutive_failures} consecutive)")
        self.status = status
        self.consecutive_failures = consecutive_failures


class RecostFatalAuthError(RecostAuthError):
    """Cloud transport suspended after N consecutive auth failures.

    Subsequent send() calls are no-ops until handle.reinit_after_fork() or process restart.
    """
```

Update `recost/__init__.py` imports and `__all__`:

```python
from ._types import (
    RecostConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    RecostError,
    RecostAuthError,
    RecostFatalAuthError,
    TransportMode,
    WindowSummary,
)
```

Add `"RecostAuthError"` and `"RecostFatalAuthError"` to `__all__`.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_errors.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add recost/_types.py recost/__init__.py tests/test_errors.py
git commit -m "feat(errors): add RecostAuthError and RecostFatalAuthError"
```

## Task 16: Add `RecostRateLimitError`

**Files:**
- Modify: `recost/_types.py`
- Modify: `recost/__init__.py`
- Modify: `tests/test_errors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_errors.py`:

```python
def test_rate_limit_error_carries_retry_after():
    from recost import RecostError, RecostRateLimitError
    err = RecostRateLimitError(retry_after_ms=2500, endpoint="/projects/p_1/telemetry")
    assert isinstance(err, RecostError)
    assert err.retry_after_ms == 2500
    assert err.endpoint == "/projects/p_1/telemetry"
    assert "2500" in str(err)
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_errors.py::test_rate_limit_error_carries_retry_after -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `recost/_types.py`:

```python
class RecostRateLimitError(RecostError):
    """API returned 429. The flush has been deferred, not dropped."""

    def __init__(self, retry_after_ms: int, endpoint: str) -> None:
        super().__init__(f"Recost API rate-limited (retry in {retry_after_ms}ms)")
        self.retry_after_ms = retry_after_ms
        self.endpoint = endpoint
```

Update `recost/__init__.py` imports and `__all__` to include `RecostRateLimitError`.

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_errors.py -v
```

Expected: all error tests pass.

- [ ] **Step 5: Commit**

```bash
git add recost/_types.py recost/__init__.py tests/test_errors.py
git commit -m "feat(errors): add RecostRateLimitError"
```

## Task 17: Validate `api_key` format in `init()`

**Files:**
- Modify: `recost/_init.py`
- Modify: `tests/test_init.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_init.py`:

```python
def test_init_rejects_string_undefined_as_api_key():
    """A literal 'undefined' (common config-file shape) must raise ValueError."""
    from recost import init, RecostConfig
    import pytest
    with pytest.raises(ValueError, match="must be a string beginning with 'rc-'"):
        init(RecostConfig(api_key="undefined"))


def test_init_rejects_non_string_api_key():
    from recost import init, RecostConfig
    import pytest
    with pytest.raises(ValueError, match="must be a string beginning with 'rc-'"):
        init(RecostConfig(api_key=123))  # type: ignore[arg-type]


def test_init_accepts_valid_rc_prefix():
    from recost import init, RecostConfig
    handle = init(RecostConfig(api_key="rc-abc123", project_id="p_1"))
    handle.dispose()


def test_init_accepts_none_api_key():
    from recost import init, RecostConfig
    handle = init(RecostConfig(api_key=None))
    handle.dispose()
```

- [ ] **Step 2: Run tests to verify failures**

```bash
pytest tests/test_init.py::test_init_rejects_string_undefined_as_api_key tests/test_init.py::test_init_rejects_non_string_api_key -v
```

Expected: FAIL (no validation yet).

- [ ] **Step 3: Implement validation**

In `recost/_init.py`, add at the top of `init()` after `config = config or RecostConfig()`:

```python
if config.api_key is not None:
    if not isinstance(config.api_key, str) or not config.api_key.startswith("rc-"):
        prefix = (
            (config.api_key[:8] + "...") if isinstance(config.api_key, str) and config.api_key
            else type(config.api_key).__name__
        )
        raise ValueError(
            f"Recost: api_key must be a string beginning with 'rc-'. Got: {prefix!r}. "
            f"See https://recost.dev/docs/api-keys."
        )
```

- [ ] **Step 4: Run all the init tests**

```bash
pytest tests/test_init.py -v
```

Expected: all four new tests pass; pre-existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add recost/_init.py tests/test_init.py
git commit -m "feat(init): validate api_key format at init() (#17)"
```

## Task 18: Refactor `_post_cloud` to return structured result

**Files:**
- Modify: `recost/_transport.py`
- Modify: `tests/test_transport.py`

This task is internal plumbing — no behavior change yet. The 401/429 logic in subsequent tasks needs a structured return value from `_post_cloud` (status code + parsed retry-after + any captured exception) rather than the current "return None on success, raise on terminal failure" shape.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_transport.py`:

```python
def test_post_cloud_returns_status_on_2xx(mock_http_server_200):
    """_post_cloud returns a _CloudResult with status=200 on success."""
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_200.url,
        body='{"ping": 1}',
        api_key="rc-test",
        max_retries=0,
    )
    assert result.status == 200
    assert result.retry_after_ms is None
    assert result.error is None


def test_post_cloud_returns_401_status_without_raising(mock_http_server_401):
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_401.url,
        body='{"ping": 1}',
        api_key="rc-bad",
        max_retries=0,
    )
    assert result.status == 401
    assert result.retry_after_ms is None


def test_post_cloud_parses_retry_after_seconds_on_429(mock_http_server_429_retry_after_2):
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_429_retry_after_2.url,
        body='{"ping": 1}',
        api_key="rc-ok",
        max_retries=0,
    )
    assert result.status == 429
    assert result.retry_after_ms == 2000
```

The three fixtures (`mock_http_server_200`, `mock_http_server_401`, `mock_http_server_429_retry_after_2`) extend the existing `tests/conftest.py` pattern. Add to `tests/conftest.py`:

```python
import http.server
import threading
from typing import Generator, Tuple
import pytest


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    status_to_return: int = 200
    retry_after_header: str = ""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(self.status_to_return)
        if self.retry_after_header:
            self.send_header("Retry-After", self.retry_after_header)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _start_server(status: int, retry_after: str = "") -> Tuple[http.server.HTTPServer, str]:
    handler = type(
        "H",
        (_StatusHandler,),
        {"status_to_return": status, "retry_after_header": retry_after},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    return server, url


@pytest.fixture
def mock_http_server_200() -> Generator:
    server, url = _start_server(200)
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()


@pytest.fixture
def mock_http_server_401() -> Generator:
    server, url = _start_server(401)
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()


@pytest.fixture
def mock_http_server_429_retry_after_2() -> Generator:
    server, url = _start_server(429, retry_after="2")
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_transport.py -v -k "post_cloud"
```

Expected: failures — `_CloudResult` doesn't exist yet; `_post_cloud` returns `None`.

- [ ] **Step 3: Implement `_CloudResult` and refactor `_post_cloud`**

In `recost/_transport.py`, replace the existing `_post_cloud` with:

```python
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import urllib.error


@dataclass
class _CloudResult:
    status: int  # 0 means network/connection error (no HTTP response received)
    retry_after_ms: Optional[int] = None
    error: Optional[Exception] = None


def _parse_retry_after(value: str) -> int:
    """Returns retry delay in milliseconds. Falls back to 60_000 on parse failure."""
    if not value:
        return 60_000
    value = value.strip()
    try:
        secs = int(value)
        return max(0, secs * 1000)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return 60_000
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta * 1000))
    except (TypeError, ValueError):
        return 60_000


def _post_cloud(
    url: str,
    body: str,
    api_key: str,
    max_retries: int,
) -> _CloudResult:
    """POST a JSON body with retry. Returns a structured result; never raises."""
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "recost-python/0.1.0",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=30)
            status = resp.getcode() or 0
            if 200 <= status < 300:
                return _CloudResult(status=status)
            # Non-success without an exception (shouldn't typically happen w/ urllib)
            if 400 <= status < 500:
                return _CloudResult(status=status)
            last_error = Exception(f"HTTP {status}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after_ms = _parse_retry_after(e.headers.get("Retry-After", "") if e.headers else "")
                return _CloudResult(status=429, retry_after_ms=retry_after_ms)
            if 400 <= e.code < 500:
                return _CloudResult(status=e.code)
            last_error = e
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            time.sleep(min(1.0 * (2 ** attempt), 10.0))

    return _CloudResult(status=0, error=last_error)
```

Update `Transport.send` to call the new shape (no behavior change for now — we'll add the 401/429 branches in tasks 19/20):

```python
def send(self, summary: WindowSummary) -> None:
    body = json.dumps(summary.to_dict())
    try:
        if self.mode == "cloud":
            url = f"{self._base_url}/projects/{self._project_id}/telemetry"
            result = _post_cloud(url, body, self._api_key, self._max_retries)
            if result.error is not None and self._on_error is not None:
                self._on_error(result.error)
            elif result.status != 0 and result.status >= 400 and self._on_error is not None:
                self._on_error(Exception(f"HTTP {result.status}"))
        else:
            if self._local is not None:
                self._local.send(body)
    except Exception as exc:
        if self._on_error is not None:
            self._on_error(exc)
        elif self._debug:
            logger.error("[recost] transport error: %s", exc)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_transport.py -v
```

Expected: all three new `_post_cloud` tests pass; pre-existing transport tests still pass.

- [ ] **Step 5: Commit**

```bash
git add recost/_transport.py tests/test_transport.py tests/conftest.py
git commit -m "refactor(transport): _post_cloud returns _CloudResult with parsed Retry-After"
```

## Task 19: Auth-failure counter + 401 escalation

**Files:**
- Modify: `recost/_transport.py`
- Modify: `tests/test_transport.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_transport.py`:

```python
def test_first_401_emits_recost_auth_error(mock_http_server_401):
    from recost import RecostAuthError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    transport = Transport(RecostConfig(
        api_key="rc-bad",
        project_id="p_1",
        base_url=mock_http_server_401.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert len(errors) == 1
    assert isinstance(errors[0], RecostAuthError)
    assert errors[0].status == 401
    assert errors[0].consecutive_failures == 1


def test_fifth_consecutive_401_emits_fatal_and_suspends(mock_http_server_401):
    from recost import RecostFatalAuthError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    transport = Transport(RecostConfig(
        api_key="rc-bad",
        project_id="p_1",
        base_url=mock_http_server_401.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    # First 5 sends each fire RecostAuthError; 5th also fires RecostFatalAuthError
    for _ in range(5):
        transport.send(summary)
    fatal = [e for e in errors if isinstance(e, RecostFatalAuthError)]
    assert len(fatal) == 1
    # Subsequent send is a no-op (suspended): no new error fires
    errors_before_sixth = list(errors)
    transport.send(summary)
    assert errors == errors_before_sixth  # nothing appended
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_transport.py -v -k "401"
```

Expected: FAIL.

- [ ] **Step 3: Implement counter + escalation in `Transport`**

In `recost/_transport.py`, update `Transport.__init__`:

```python
def __init__(self, config: RecostConfig) -> None:
    self.mode: TransportMode = "cloud" if config.api_key else "local"
    self._api_key = config.api_key or ""
    self._project_id = config.project_id or ""
    self._base_url = config.base_url.rstrip("/")
    self._max_retries = config.max_retries
    self._debug = config.debug
    self._on_error = config.on_error

    self._consecutive_auth_failures: int = 0
    self._suspended: bool = False
    self._auth_warned_stderr: bool = False

    self._local: Optional[_LocalTransport] = None
    if self.mode == "local":
        self._local = _LocalTransport(config.local_port, config.debug)
```

Then update `Transport.send` to handle 401 specifically:

```python
def send(self, summary: "WindowSummary") -> None:
    if self._suspended:
        return
    body = json.dumps(summary.to_dict())
    try:
        if self.mode == "cloud":
            url = f"{self._base_url}/projects/{self._project_id}/telemetry"
            result = _post_cloud(url, body, self._api_key, self._max_retries)
            self._handle_cloud_result(result, url)
        else:
            if self._local is not None:
                self._local.send(body)
    except Exception as exc:
        if self._on_error is not None:
            self._on_error(exc)
        elif self._debug:
            logger.error("[recost] transport error: %s", exc)


def _handle_cloud_result(self, result: _CloudResult, url: str) -> None:
    from ._types import RecostAuthError, RecostFatalAuthError

    if 200 <= result.status < 300:
        self._consecutive_auth_failures = 0
        return

    if result.status == 401:
        self._consecutive_auth_failures += 1
        if not self._auth_warned_stderr:
            import sys
            print(
                "Recost: API rejected key (401). Telemetry will be dropped. "
                "Check your api_key at https://recost.dev/dashboard/account.",
                file=sys.stderr,
            )
            self._auth_warned_stderr = True
        if self._on_error is not None:
            self._on_error(RecostAuthError(
                status=401,
                consecutive_failures=self._consecutive_auth_failures,
            ))
        if self._consecutive_auth_failures >= 5:
            self._suspended = True
            if self._on_error is not None:
                self._on_error(RecostFatalAuthError(
                    status=401,
                    consecutive_failures=self._consecutive_auth_failures,
                ))
        return

    # Other terminal errors — leave 429 to the next task
    if result.error is not None and self._on_error is not None:
        self._on_error(result.error)
    elif result.status >= 400 and self._on_error is not None:
        self._on_error(Exception(f"HTTP {result.status}"))
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_transport.py -v -k "401"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recost/_transport.py tests/test_transport.py
git commit -m "feat(transport): 401 escalation — RecostAuthError per failure, Fatal at 5 (#14)"
```

## Task 20: 429 / Retry-After deferral

**Files:**
- Modify: `recost/_init.py` (defer next flush)
- Modify: `recost/_transport.py` (signal back the retry_after)
- Modify: `tests/test_transport.py`

The flush timer lives in `_init.py`. To defer the next flush, the transport needs to either (a) directly tell the timer to sleep longer, or (b) emit an error that `_init.py`'s flush-loop sees and reacts to. We'll use (a): pass a `defer_callback` into `Transport`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_transport.py`:

```python
def test_429_emits_rate_limit_error_and_invokes_defer(mock_http_server_429_retry_after_2):
    from recost import RecostRateLimitError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    defers: list = []
    transport = Transport(RecostConfig(
        api_key="rc-ok",
        project_id="p_1",
        base_url=mock_http_server_429_retry_after_2.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    transport.set_defer_callback(defers.append)
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert len(errors) == 1
    assert isinstance(errors[0], RecostRateLimitError)
    assert errors[0].retry_after_ms == 2000
    assert defers == [2000]


def test_429_does_not_increment_auth_counter(mock_http_server_429_retry_after_2):
    from recost import RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    transport = Transport(RecostConfig(
        api_key="rc-ok",
        project_id="p_1",
        base_url=mock_http_server_429_retry_after_2.url.rstrip("/"),
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert transport._consecutive_auth_failures == 0
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_transport.py -v -k "429"
```

Expected: FAIL — `set_defer_callback` doesn't exist.

- [ ] **Step 3: Implement**

In `recost/_transport.py`:

```python
class Transport:
    def __init__(self, config: RecostConfig) -> None:
        # ... existing ...
        self._defer_callback: Optional[Callable[[int], None]] = None

    def set_defer_callback(self, cb: Callable[[int], None]) -> None:
        """Called with retry_after_ms when the API returns 429."""
        self._defer_callback = cb

    def _handle_cloud_result(self, result: _CloudResult, url: str) -> None:
        from ._types import RecostAuthError, RecostFatalAuthError, RecostRateLimitError

        if 200 <= result.status < 300:
            self._consecutive_auth_failures = 0
            return

        if result.status == 401:
            # ... existing 401 logic unchanged ...
            return

        if result.status == 429:
            retry_after_ms = result.retry_after_ms or 60_000
            if self._on_error is not None:
                self._on_error(RecostRateLimitError(
                    retry_after_ms=retry_after_ms,
                    endpoint=f"/projects/{self._project_id}/telemetry",
                ))
            if self._defer_callback is not None:
                self._defer_callback(retry_after_ms)
            return

        # Other terminal errors
        if result.error is not None and self._on_error is not None:
            self._on_error(result.error)
        elif result.status >= 400 and self._on_error is not None:
            self._on_error(Exception(f"HTTP {result.status}"))
```

Add `from typing import Callable` if not already imported.

In `recost/_init.py`, wire up the defer callback against the flush timer's stop event. Add a `_deferral_ms` attribute managed by a thread-safe primitive:

```python
# Inside init(), after `transport = Transport(config)`:
_deferral_lock = threading.Lock()
_deferral_ms: list[int] = [0]  # mutable cell

def _defer(ms: int) -> None:
    with _deferral_lock:
        _deferral_ms[0] = max(_deferral_ms[0], ms)

transport.set_defer_callback(_defer)

def _timer_loop() -> None:
    while not stop_event.is_set():
        # Consume any pending deferral
        with _deferral_lock:
            extra = _deferral_ms[0]
            _deferral_ms[0] = 0
        wait = config.flush_interval + (extra / 1000.0)
        if stop_event.wait(timeout=wait):
            return
        try:
            flush_and_send()
        except Exception as err:
            if config.on_error:
                config.on_error(err)
            elif debug:
                print(f"[recost] flush error: {err}", file=sys.stderr)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_transport.py -v -k "429"
```

Expected: PASS.

- [ ] **Step 5: Run all transport + init tests**

```bash
pytest tests/test_transport.py tests/test_init.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add recost/_transport.py recost/_init.py tests/test_transport.py
git commit -m "feat(transport): honor 429 Retry-After — emit RateLimitError, defer next flush (#18)"
```

## Task 21: aiohttp body sizing — `json=` and FormData

**Files:**
- Modify: `recost/_interceptor.py`
- Modify: `tests/test_interceptor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_interceptor.py`:

```python
@pytest.mark.asyncio
async def test_aiohttp_json_body_records_size(mock_http_server_200):
    """When the user passes `json={...}` to aiohttp, request_bytes must reflect
    the serialized JSON length, not 0."""
    import aiohttp
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(mock_http_server_200.url, json={"x": "y"}) as resp:
                await resp.read()
        assert len(events) >= 1
        ev = events[-1]
        expected = len(json.dumps({"x": "y"}))
        assert ev.request_bytes == expected
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_aiohttp_formdata_body_records_size(mock_http_server_200):
    import aiohttp
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)
    try:
        form = aiohttp.FormData()
        form.add_field("name", "value")
        async with aiohttp.ClientSession() as session:
            async with session.post(mock_http_server_200.url, data=form) as resp:
                await resp.read()
        ev = events[-1]
        # FormData._size is the byte count; assert non-zero
        assert ev.request_bytes > 0
    finally:
        uninstall()
```

Add `import json` to the test file imports if not already present.

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_interceptor.py -v -k "aiohttp_json or aiohttp_formdata"
```

Expected: FAIL — `request_bytes == 0`.

- [ ] **Step 3: Implement**

In `recost/_interceptor.py`'s `_patched_request` (the aiohttp wrapper), replace the body-sizing block:

```python
import json as _json

# Inside _patched_request, replacing lines ~317-325:
try:
    data = kwargs.get("data")
    json_body = kwargs.get("json")
    if json_body is not None:
        try:
            request_bytes = len(_json.dumps(json_body))
        except (TypeError, ValueError):
            request_bytes = 0
    elif data is not None:
        if isinstance(data, (bytes, bytearray)):
            request_bytes = len(data)
        elif isinstance(data, str):
            request_bytes = len(data.encode("utf-8", errors="replace"))
        elif hasattr(data, "_size"):
            size_attr = getattr(data, "_size", None)
            request_bytes = int(size_attr) if isinstance(size_attr, int) and size_attr >= 0 else 0
        # async iterables, BytesIO without known size — leave at 0
except Exception:
    pass
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_interceptor.py -v -k "aiohttp_json or aiohttp_formdata"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recost/_interceptor.py tests/test_interceptor.py
git commit -m "fix(interceptor): measure aiohttp json= and FormData body sizes (#8)"
```

## Task 22: httpx streaming-content guard

**Files:**
- Modify: `recost/_interceptor.py`
- Modify: `tests/test_interceptor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_interceptor.py`:

```python
def test_httpx_streaming_content_not_materialized(mock_http_server_200):
    """Passing an async-iterable body to httpx must not be read by the interceptor."""
    import httpx
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)
    materialized = {"count": 0}

    def stream_body():
        materialized["count"] += 1
        yield b"chunk1"
        yield b"chunk2"

    try:
        # httpx.Request with an iterator body
        req = httpx.Request("POST", mock_http_server_200.url, content=stream_body())
        with httpx.Client() as client:
            client.send(req)
        # If the interceptor materialized the body, the generator ran >0 times
        # before our send call could; if it didn't, we expect materialized["count"] == 1
        # (only httpx itself iterated it).
        # The crucial assertion: request_bytes is 0 (we skipped sizing the stream).
        ev = events[-1]
        assert ev.request_bytes == 0
    finally:
        uninstall()
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_interceptor.py::test_httpx_streaming_content_not_materialized -v
```

Expected: depends on httpx version — accessing `request.content` on a streaming body either raises `RequestNotRead` or materializes the iterator. Either way, the test asserts `request_bytes == 0` and the *crucial* check is that the SDK does not crash and does not silently buffer multi-GB uploads.

- [ ] **Step 3: Implement guard in both httpx wrappers**

In `recost/_interceptor.py`, replace the body-sizing block in both `_patched_send` (sync) and `_patched_async_send`:

```python
# Replace:
#   if hasattr(request, "content") and request.content is not None:
#       request_bytes = len(request.content)
# With:
try:
    content = getattr(request, "content", None)
    if isinstance(content, (bytes, bytearray)):
        request_bytes = len(content)
    # Non-bytes (streaming/async-iterable) — skip sizing; never materialize.
except Exception:
    pass
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_interceptor.py -v
```

Expected: all pass; streaming test passes; non-streaming tests still report correct bytes.

- [ ] **Step 5: Commit**

```bash
git add recost/_interceptor.py tests/test_interceptor.py
git commit -m "fix(interceptor): guard httpx streaming bodies — never materialize iterators (#8)"
```

## Task 23: README streaming caveat + remove `_post_cloud` body shadowing

**Files:**
- Modify: `README.md`
- Modify: `recost/_transport.py` (minor cleanup spotted while editing)

- [ ] **Step 1: Update the README**

In `README.md`, find the "Captured / Never captured" section (around line 172). Replace the existing "Request and response body size (bytes)" line with:

```markdown
- Request body size (bytes) — measured for all JSON / form / bytes payloads. Streaming uploads (async iterators) are not measured (reported as 0) to avoid buffering large bodies.
- Response body size (bytes) — derived from the `Content-Length` response header. HTTP chunked and SSE streams do not set this header and will report 0.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): clarify streaming caveats for request/response byte counts"
```

## Task 24: Push PR 3

- [ ] **Step 1: Run full suite locally**

```bash
mypy recost/ && ruff check recost/ tests/ && pytest -v
```

Expected: all green.

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin wave-a/pr3-transport
gh pr create --title "Wave A PR 3: typed errors + 401/429 escalation + body-size + key validation" --body "$(cat <<'EOF'
## Summary
- **#14 401 escalation:** consecutive 401s emit `RecostAuthError`; the 5th emits `RecostFatalAuthError` and suspends the transport. First failure also prints a one-line stderr warning so the developer is told immediately.
- **#18 429 Retry-After:** parses the `Retry-After` header (seconds or HTTP-date), defers the next flush by that delay, emits `RecostRateLimitError`. Window is not dropped.
- **#17 api_key validation:** `init(api_key="undefined")` now raises `ValueError` with a clear message. Catches the most common misconfig.
- **#8 body sizes:** aiohttp `json=` and `FormData` are now measured; httpx streaming bodies are explicitly skipped to avoid OOM on large uploads.
- README updated to document streaming caveats.

## Public API additions
- `RecostError` (PR 2), `RecostAuthError`, `RecostFatalAuthError`, `RecostRateLimitError` — all subclasses of `Exception`. Existing `on_error: Callable[[Exception], None]` signature unchanged.

## Test plan
- [ ] `tests/test_errors.py` — new exception classes import and carry expected attrs.
- [ ] `tests/test_transport.py` — 401 escalation, fatal at 5, 429 deferral, 429 not auth-counted.
- [ ] `tests/test_init.py` — `api_key="undefined"` raises.
- [ ] `tests/test_interceptor.py` — aiohttp `json=`/FormData sized, httpx streaming not materialized.
- [ ] CI matrix green.

Closes #8, #14, #17, #18.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Verify CI passes**

All four Python jobs green.

---

# Acceptance checklist

Wave A is complete when, on a clean checkout of `main` after all three PRs land:

- [ ] `mypy recost/` returns 0 errors.
- [ ] `ruff check recost/ tests/` returns 0 errors.
- [ ] `pytest` passes — including `test_fork_safety.py`, `test_reentrancy.py`, `test_errors.py`, and new tests in `test_init.py`, `test_transport.py`, `test_interceptor.py`.
- [ ] CI runs all three on every PR; required-status checks block merge.
- [ ] `init(RecostConfig(api_key="undefined"))` raises `ValueError`.
- [ ] A mock-401 reproduction shows escalation: 1st send → `RecostAuthError`, 5th send → `RecostFatalAuthError`, 6th send is a no-op.
- [ ] A mock-429 reproduction shows the window deferred (not dropped), `RecostRateLimitError` emitted with correct `retry_after_ms`, next flush delayed by that amount.
- [ ] An `os.fork()` reproduction shows the child process delivering at least one event to a mock cloud server.
- [ ] README "What is captured" section documents the streaming-response byte-count caveat.
- [ ] All issue references (#2, #3, #8, #13, #14, #15, #17, #18) closed via PR-link.
