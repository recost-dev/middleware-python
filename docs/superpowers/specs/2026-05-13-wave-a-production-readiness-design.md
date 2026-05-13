# Wave A — Production-Readiness Design

**Date:** 2026-05-13
**Status:** Approved for planning
**Scope:** `middleware-python` (this repo only)
**Tracking issues:** #2, #3, #8, #13, #14, #15, #17, #18

## Goal

Close every silent-failure gap that prevents `recost` from being safely deployed to production. Today the SDK reports zero telemetry under the most common production deploy shape (gunicorn / Celery prefork), can double-count requests in mixed sync/async stacks, silently drops data forever on a revoked API key, ignores rate-limit signals, and ships with public claims (mypy strict-clean, full aiohttp support) that are not true.

After Wave A, a user who follows the README can put this SDK into a real production application — including prefork servers, mixed httpx+requests stacks, and rotated API keys — and trust both that data is reaching the backend and that they will be told when it isn't.

## Non-goals

- Node SDK changes. Node-parity tracking items (#16, #19, #20, #21, #22, #23) are explicitly **not** in this wave, even where the Python side overlaps. They land in a later wave with coordinated server-side and Node-side changes.
- Provider-pricing freshness (#24). Cost numbers stay as they are.
- The local-WebSocket hardening pile (#7). Out of scope; lands with the coordinated extension-side protocol work.
- Test gaps beyond what Wave A's own changes require (#9). Wave A adds tests for every new behavior it ships, but does not retroactively backfill the pre-existing aiohttp / privacy / 5xx-retry test gaps.

## Public API changes

### New exception hierarchy

In `recost/_types.py` (re-exported from `recost/__init__.py`):

```python
class RecostError(Exception):
    """Base class for typed SDK errors passed to on_error."""


class RecostAuthError(RecostError):
    """API rejected the configured api_key (401)."""

    def __init__(self, status: int, consecutive_failures: int, message: str = "") -> None:
        super().__init__(message or f"Recost API returned {status} (auth failed)")
        self.status = status
        self.consecutive_failures = consecutive_failures


class RecostFatalAuthError(RecostAuthError):
    """Cloud transport suspended after N consecutive auth failures.

    Subsequent send() calls are no-ops until reinit / reconfigure.
    """


class RecostRateLimitError(RecostError):
    """API returned 429. The flush has been deferred, not dropped."""

    def __init__(self, retry_after_ms: int, endpoint: str) -> None:
        super().__init__(f"Recost API rate-limited (retry in {retry_after_ms}ms)")
        self.retry_after_ms = retry_after_ms
        self.endpoint = endpoint
```

The existing `on_error: Callable[[Exception], None]` signature is unchanged. Existing user code keeps working because the new classes are `Exception` subclasses. Users who want to branch use `isinstance`.

### New handle method

```python
class RecostHandle:
    def reinit_after_fork(self) -> None:
        """Recreate timer thread, transport thread, and event loop in the current PID.

        Called automatically via os.register_at_fork on platforms that support it.
        Provided as a manual escape hatch for uwsgi lazy-fork or other edge cases.
        """
```

### New init-time validation

`init()` raises `ValueError` immediately if `api_key` is set and isn't a non-empty string starting with `"rc-"`. Error message includes a safe prefix of the bad value (first 8 chars) and a link to where to find the real key.

```python
if config.api_key is not None:
    if not isinstance(config.api_key, str) or not config.api_key.startswith("rc-"):
        prefix = config.api_key[:8] if isinstance(config.api_key, str) else type(config.api_key).__name__
        raise ValueError(
            f"Recost: api_key must be a string beginning with 'rc-'. Got: {prefix!r}. "
            f"See https://recost.dev/docs/api-keys."
        )
```

## Architecture changes

### Fork-safety (#3) — hybrid

**Three layers of defense, in this order:**

1. **`os.register_at_fork(after_in_child=_after_fork)`** registered during `init()`. The hook rebuilds: flush timer thread, transport thread, local-mode asyncio event loop. State that's safe to keep: `config`, `registry`, the patched interceptor functions (they're class-level, fork-safe). State that's reset: the in-memory event buffer (parent's events are not the child's responsibility; the parent process flushes them).

2. **PID backstop in every interceptor entry.** On each intercepted call, compare `os.getpid()` to `_handle.pid`. If mismatch (e.g., a uwsgi lazy-fork environment where `register_at_fork` doesn't run), emit `RecostError("recost not initialized in PID X; call handle.reinit_after_fork()")` via `on_error` **at most once per PID**, and refuse to record events until `reinit_after_fork()` is called.

3. **Public `handle.reinit_after_fork()`** for explicit reinit. Idempotent.

The PID backstop ensures we never double-send (parent + child both flushing the same buffer) and never crash on a `run_coroutine_threadsafe` against a parent's dead loop. The `register_at_fork` hook ensures the common case (`os.fork()` based prefork) just works.

### Reentrancy guard (#15) — task + thread

Current `_interceptor.py` uses only a `ContextVar`. ContextVars are async-task-scoped; they do not propagate when work hops to a different OS thread (`asyncio.to_thread`, `ThreadPoolExecutor`, library-internal thread pools). Result: a single outbound HTTP call routed through both an async patch and a sync patch (e.g., httpx async → urllib3 sync via a thread pool) is counted twice.

**Fix:** track both axes.

```python
_in_interceptor_task: ContextVar[bool] = ContextVar("_in_interceptor_task", default=False)
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

Every patch wrapper (urllib3, httpx sync, httpx async, aiohttp) routes through `_enter_interceptor` / `_exit_interceptor` in a `try/finally`.

### Cloud transport behavior

#### 401 escalation (#14)

In `_transport.py`'s cloud send path:

- Maintain `_consecutive_auth_failures: int` on the transport instance.
- On HTTP 401:
  - Increment counter.
  - On the **first** 401: log a single line to stderr (not the debug-only path): `"Recost: API rejected key (401). Telemetry will be dropped. Check your api_key at https://recost.dev/dashboard/account."`
  - Emit `RecostAuthError(status=401, consecutive_failures=N)` via `on_error`.
  - Drop the window (no retry, 4xx is non-retriable).
- After `_consecutive_auth_failures >= 5`:
  - Emit `RecostFatalAuthError(status=401, consecutive_failures=N)` via `on_error`.
  - Set `self._suspended = True`. Subsequent `send()` calls are no-ops (silent — the user has been told).
  - Resume only on `handle.reconfigure(...)` (out of scope for Wave A — leave `_suspended` reset to manual restart for now).
- On a successful (2xx) flush, reset `_consecutive_auth_failures = 0`.

#### 429 with Retry-After (#18)

- On HTTP 429:
  - Parse `Retry-After` header. Accept integer seconds; if it's an HTTP-date, parse via `email.utils.parsedate_to_datetime`; on parse failure, default to 60 seconds.
  - Compute `retry_after_ms`.
  - **Do not drop the window.** Re-queue it onto the aggregator's pending list to be sent with the next flush.
  - Defer the next scheduled flush by `retry_after_ms`.
  - Emit `RecostRateLimitError(retry_after_ms=N, endpoint=path)` via `on_error`.
  - Reset `_consecutive_auth_failures` (429 ≠ 401).

#### Other 4xx, 5xx, network errors

Unchanged from today, except errors are now wrapped as `RecostError` subclasses where they map naturally.

## Interceptor body sizing (#8)

In `_interceptor.py`, replace the current narrow checks with shape-aware measurement:

### aiohttp

In `_patched_request`:

```python
def _measure_aiohttp_body(kwargs: dict) -> int:
    data = kwargs.get("data")
    json_body = kwargs.get("json")
    if json_body is not None:
        try:
            return len(json.dumps(json_body))
        except (TypeError, ValueError):
            return 0
    if data is None:
        return 0
    if isinstance(data, (bytes, bytearray)):
        return len(data)
    if isinstance(data, str):
        return len(data.encode("utf-8"))
    if hasattr(data, "_size"):  # FormData
        size = getattr(data, "_size", None)
        return int(size) if isinstance(size, int) and size >= 0 else 0
    return 0  # async iterables, BytesIO without a fixed size — leave unmeasured
```

### httpx (sync + async)

Guard against materializing streaming bodies:

```python
content = getattr(request, "content", None)
if isinstance(content, bytes):
    request_bytes = len(content)
else:
    request_bytes = 0  # streaming/async-iterable body; never read it
```

### Response sizes

No code change. Add to README under "What is captured":

> Response byte counts are derived from the `Content-Length` response header. Streaming responses (HTTP chunked, SSE) do not set this header and will report `response_bytes = 0`.

## mypy + CI (#2, #13)

### mypy

Fix all 35 errors. Key categories:

- `latency_ms: int` parameters receiving `float` from `(time.perf_counter() - start) * 1000` → either type the parameter as `float` or `int(...)` at the call site. Choose `float` everywhere — latency is naturally fractional.
- `frameworks/flask.py:33` `self._handle = None` infers `None`; annotate as `self._handle: Optional[RecostHandle] = None`.
- Stale `# type: ignore` → remove (or replace with a narrower `# type: ignore[name]` where still needed for optional-dep import boundaries).

### CI

New `.github/workflows/ci.yml`:

- Trigger: `pull_request` and `push` to `main`.
- Matrix: Python 3.9, 3.10, 3.11, 3.12.
- Steps: install with `pip install -e ".[dev,all]"`, run `ruff check recost/ tests/`, run `mypy recost/`, run `pytest`.
- Required-status checks: all four Python versions must pass for merge.

### Drive-by hygiene (#13)

- Remove unused `MAX_BUCKETS` import in `_transport.py`.
- Replace every `raise exc` with bare `raise` to preserve traceback. Affects `_interceptor.py` at lines 148–156, 219–228, 266–275, 342–350.

## Implementation sequencing — three PRs

### PR 1 — Foundation

- Add `.github/workflows/ci.yml`.
- Fix 35 mypy errors.
- Hygiene fixes (#13).
- No behavior changes.

**Rationale:** lands first to guarantee subsequent PRs land green and to make the README claim true before any behavior change.

### PR 2 — Lifecycle & correctness

- `RecostError` base class (needed by the PID backstop in this PR; subclasses land in PR 3).
- Fork-safety (#3): `register_at_fork`, PID backstop, `handle.reinit_after_fork()`.
- Reentrancy guard (#15): contextvar + threading.local.
- Tests: subprocess-based fork test, mixed-thread reentrancy regression test.

**Rationale:** internal-only changes with the highest blast-radius if wrong; keep them isolated from the user-visible API additions in PR 3.

### PR 3 — Transport & validation

- `RecostAuthError`, `RecostFatalAuthError`, `RecostRateLimitError` (subclasses of the base added in PR 2).
- 401 escalation (#14).
- 429 / Retry-After (#18).
- API key format validation (#17).
- Interceptor body-size fixes (#8).
- Tests: auth escalation, 429 deferral, API-key-validation error, aiohttp `json=` body size.

**Rationale:** user-visible behavior changes ride together so release notes can describe them as one batch.

## Test plan

Each PR ships its own tests. Highlights:

### Fork safety

```python
def test_fork_safety(mock_cloud_server, tmp_pid_file):
    init(RecostConfig(api_key="rc-test", base_url=mock_cloud_server.url))
    pid = os.fork()
    if pid == 0:
        # child
        requests.get("https://api.openai.com/v1/models")
        time.sleep(0.5)  # let flush fire
        os._exit(0)
    os.waitpid(pid, 0)
    assert mock_cloud_server.received_event_for_pid(pid)
```

### Reentrancy regression

```python
async def test_no_double_count_async_to_thread(mock_server):
    init(...)
    await asyncio.gather(*[
        asyncio.to_thread(requests.get, mock_server.url) for _ in range(10)
    ])
    handle.flush_sync()
    assert mock_server.events_received == 10  # not 20
```

### Auth escalation

```python
def test_auth_failure_escalates(mock_401_server):
    errors: list[Exception] = []
    init(RecostConfig(api_key="rc-bad", base_url=mock_401_server.url, on_error=errors.append))
    for _ in range(5):
        requests.get("https://api.openai.com/v1/models")
        force_flush()
    assert isinstance(errors[0], RecostAuthError)
    assert isinstance(errors[-1], RecostFatalAuthError)
    # Subsequent send is a no-op
    mock_401_server.reset_request_count()
    requests.get("https://api.openai.com/v1/models")
    force_flush()
    assert mock_401_server.request_count == 0
```

### 429 honored

```python
def test_429_defers_flush(mock_429_server_with_retry_after):
    errors: list[Exception] = []
    init(RecostConfig(api_key="rc-ok", base_url=mock_429_server.url, on_error=errors.append))
    requests.get("https://api.openai.com/v1/models")
    force_flush()
    assert len(errors) == 1
    assert isinstance(errors[0], RecostRateLimitError)
    assert errors[0].retry_after_ms == 2000
    # Window was re-queued, not dropped — next flush after retry_after delivers it
```

### API key validation

```python
def test_init_rejects_undefined_api_key():
    with pytest.raises(ValueError, match="must be a string beginning with 'rc-'"):
        init(RecostConfig(api_key="undefined"))
```

### aiohttp body sizing

```python
async def test_aiohttp_json_body_size(mock_server):
    init(...)
    async with aiohttp.ClientSession() as session:
        await session.post(mock_server.url, json={"x": "y"})
    handle.flush_sync()
    assert mock_server.last_event.request_bytes == len('{"x": "y"}')
```

## Acceptance criteria

Wave A is done when, on a clean checkout:

- `mypy recost/` returns 0 errors.
- `ruff check recost/ tests/` returns 0 errors.
- `pytest` passes including all new tests above.
- CI runs all three on every PR and is required for merge.
- A scripted `os.fork()` reproduction (in `tests/test_fork_safety.py`) shows the child process successfully delivering at least one event to a mock cloud server.
- `init(RecostConfig(api_key="undefined"))` raises `ValueError` (was: silently entered cloud mode and 401'd forever).
- A mock-401 reproduction shows escalation from `RecostAuthError` (call 1) to `RecostFatalAuthError` (call 5) with no telemetry sent after suspension.
- A mock-429 reproduction shows the window deferred (not dropped) and `RecostRateLimitError` emitted with the correct `retry_after_ms`.
- README "What is captured" section documents the streaming-response byte-count caveat.
- README no longer claims mypy strict-clean unless it actually is (it will be).

## Risks and mitigations

- **`os.register_at_fork` is POSIX-only.** Windows: skip the hook registration, fall back to PID backstop + manual `reinit_after_fork()`. The README explicitly does not promise Windows production support today, so this is acceptable.
- **`threading.local` adds overhead on the hot path.** Negligible (single attribute access per request), but worth measuring once — add a benchmark in PR 2 that times 10k requests through the patched stack with and without the guard.
- **Re-queueing 429'd windows can grow memory if the server stays 429.** Cap the deferred-window queue at the same MAX_BUCKETS-equivalent and drop the oldest with a `RecostError` if exceeded. Add as a stretch task in PR 3.
- **The `register_at_fork` hook fires for every fork, even short-lived `multiprocessing.Pool` workers.** Acceptable — the child just spins up a daemon thread that exits with the worker. No leaked state because daemon threads die with the process.

## Out of scope (for future waves)

- All Node-parity items not directly required to fix a P0/P1 (#16, #19, #20, #21, #22, #23) — Wave C territory.
- Local-WS hardening (#7) — needs coordinated extension-side protocol work.
- Generic test backfill (#9) — Wave B.
- Exclude-pattern semantics rework (#12) — Wave B.
- Provider pricing source-of-truth (#24) — Wave C / pricing-sync work.
