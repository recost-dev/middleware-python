# Wave B-1 — Test Fortification & Privacy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every README-promised behavior in `recost` (Python middleware) that ships with zero direct test coverage, and harden `_strip_query` to use `urllib.parse.urlparse` so the privacy contract is enforced in code, not convention.

**Architecture:** Two PRs, both branched off `origin/main` (do **not** chain PRs). PR-A bundles the one-line `_strip_query` fix with all non-missing-dep tests. PR-B is two small tests that fake missing optional deps via `sys.modules` monkeypatching plus `importlib.reload` — different test shape, isolated PR to limit blast radius.

**Tech Stack:** Python ≥ 3.9, pytest, pytest-asyncio, ruff, mypy (strict), hatchling. Standard library `urllib.parse`. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-13-wave-b1-test-fortification-design.md`.

---

## File Structure

### Files modified

- `recost/_interceptor.py` — `_strip_query` function (lines 76–84): substring-based implementation replaced with `urllib.parse.urlparse`-based one. **One function, ~12 lines of net diff.** No other code in the module changes.
- `tests/test_interceptor.py` — extended `_Handler` (route 404 for `/notfound`); new `_find_free_port` helper; new `TestPrivacy` class (6 tests); new `TestAiohttp` class (4 tests). **Three additions, all at the bottom of the file or in the existing handler.**
- `tests/test_transport.py` — new `TestSelfInstrumentation` class (1 test); new `Test5xxRetry` class (2 tests). **Two additions, appended.**
- `tests/test_init.py` — new test `test_legacy_flush_interval_emits_deprecation_warning`; new test `test_dispose_prevents_further_flushes`. **Two standalone functions appended.**
- `tests/test_aggregator.py` — new test `test_overflow_triggers_early_flush`. **One standalone function appended.**
- `tests/test_flask.py` — *(PR-B only)* new test `test_flask_extension_raises_clear_error_without_flask`. **One standalone function appended.**

### Files created

None — every test lands in an existing test module that already covers the module under test.

### Why this shape

Test backfill goes next to the existing tests for the same module so future readers find related coverage in one place. The only production-code change is the `_strip_query` rewrite, which is tightly scoped (one function in one file). No new abstractions, no new modules, no refactoring.

---

## PR-A — Test fortification + privacy fix

PR-A branch: `wave-b1/pr-a-test-fortification`, off `origin/main`.

### Task 1: Branch setup

**Files:** none modified yet.

- [ ] **Step 1: Verify clean working tree on the wave-b1 spec branch**

```bash
git status -uno
git branch --show-current
```

Expected: branch is `wave-b1/spec-plan` (or whatever the spec-writing branch is named), no uncommitted production code. `.pyc` modifications are fine — they aren't tracked and are harmless.

- [ ] **Step 2: Fetch and create the PR-A branch off origin/main**

```bash
git fetch origin
git checkout -b wave-b1/pr-a-test-fortification origin/main
```

Expected: switched to a new branch tracking nothing (we'll set upstream on first push).

- [ ] **Step 3: Install dev dependencies and confirm the baseline is green**

```bash
pip install -e ".[dev,all]"
pytest -q
mypy recost/
ruff check recost/ tests/
```

Expected: every check passes on a clean `origin/main`. If anything fails on `origin/main`, **stop** — investigate before adding any new tests on top of red.

---

### Task 2: Test-infra helpers in `tests/test_interceptor.py`

Mechanical preparation — extends the existing handler for 404 and adds a free-port helper inline.

**Files:**
- Modify: `tests/test_interceptor.py` (the `_Handler` class around line 22, and append `_find_free_port` after the `test_server` fixture around line 49).

- [ ] **Step 1: Extend `_Handler.do_GET` to branch on `/notfound`**

Replace the existing `do_GET`:

```python
    def do_GET(self):
        if self.path == "/notfound":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")
```

- [ ] **Step 2: Add a port helper after the existing `test_server` fixture**

Append after `test_server` (before any class):

```python
def _find_free_port() -> int:
    """Bind an ephemeral TCP socket and immediately release it.

    The returned port is very likely free for ~µs after this call, which is
    enough for a single aiohttp connect attempt that we *want* to fail.
    """
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
```

- [ ] **Step 3: Run the existing test module — should be unchanged**

```bash
pytest tests/test_interceptor.py -q
```

Expected: same pass/skip count as on `origin/main`. The new handler branch is unused by existing tests; the new helper isn't imported anywhere yet.

- [ ] **Step 4: Commit**

```bash
git add tests/test_interceptor.py
git commit -m "$(cat <<'EOF'
test(interceptor): extend _Handler for 404 path + add _find_free_port helper

Wave B-1 preparation. The aiohttp non-2xx test will hit /notfound; the
exception-path test needs a closed port to connect to.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `TestPrivacy` — 6 tests in `tests/test_interceptor.py`

Three tests are direct unit tests of `_strip_query`; three are end-to-end checks against `mock_http_server_200`. Some will **fail on `origin/main`** (the fragment + userinfo cases) — that's expected; Task 4 fixes them.

**Files:**
- Modify: `tests/test_interceptor.py` — append a `TestPrivacy` class at the bottom.

- [ ] **Step 1: Append the test class**

```python
# ---------------------------------------------------------------------------
# Privacy contract (#9 + #19)
# ---------------------------------------------------------------------------


class TestPrivacy:
    """The README promises that headers and bodies are never captured and that
    query strings are stripped. These tests enforce that contract directly."""

    def test_raw_event_has_no_headers_field(self):
        """RawEvent must not contain any header-shaped field, now or later."""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(RawEvent)}
        for forbidden in ("headers", "request_headers", "response_headers"):
            assert forbidden not in field_names, (
                f"RawEvent has a forbidden field {forbidden!r}; "
                f"adding header capture would break the privacy contract"
            )

    def test_authorization_header_never_leaves_a_trace(self, mock_http_server_200):
        """A request with Authorization: Bearer <secret> must not leak that
        secret into any captured field."""
        import dataclasses
        import requests as _requests
        events: list[RawEvent] = []
        install(events.append)
        try:
            _requests.get(
                mock_http_server_200.url,
                headers={"Authorization": "Bearer secret123"},
            )
            assert len(events) >= 1
            for value in dataclasses.asdict(events[-1]).values():
                assert "secret123" not in str(value), (
                    f"Authorization secret leaked into a RawEvent field: {value!r}"
                )
        finally:
            uninstall()

    def test_query_string_never_leaves_a_trace(self, mock_http_server_200):
        """A request with ?api_key=<secret> must not leak the secret into any
        captured field — url should be stripped of the query, and no other
        field should carry the raw query string."""
        import dataclasses
        import requests as _requests
        events: list[RawEvent] = []
        install(events.append)
        try:
            _requests.get(mock_http_server_200.url + "?api_key=topsecret")
            assert len(events) >= 1
            for value in dataclasses.asdict(events[-1]).values():
                assert "topsecret" not in str(value), (
                    f"Query string secret leaked into a RawEvent field: {value!r}"
                )
        finally:
            uninstall()

    def test_strip_query_handles_encoded_question_mark(self):
        """A path with %3F before the real ? must keep the encoded %3F
        intact and strip only the real query string."""
        from recost._interceptor import _strip_query
        result = _strip_query("https://api.example.com/p%3Fath?real=secret")
        assert result == "https://api.example.com/p%3Fath"
        assert "secret" not in result

    def test_strip_query_drops_fragment(self):
        """URL fragments are dropped along with the query string — both can
        carry sensitive context."""
        from recost._interceptor import _strip_query
        assert _strip_query("https://h/p#frag") == "https://h/p"

    def test_strip_query_drops_userinfo(self):
        """URL userinfo (user:pass@) is a credential leak — it must not
        survive _strip_query."""
        from recost._interceptor import _strip_query
        result = _strip_query("https://user:pass@h/p")
        assert result == "https://h/p"
        assert "user" not in result
        assert "pass" not in result
```

- [ ] **Step 2: Run the new tests — record which fail**

```bash
pytest tests/test_interceptor.py::TestPrivacy -v
```

Expected on `origin/main`:
- `test_raw_event_has_no_headers_field` — PASS (RawEvent has no headers field today).
- `test_authorization_header_never_leaves_a_trace` — PASS (the SDK never captures headers; this test pins that in place).
- `test_query_string_never_leaves_a_trace` — PASS (`?api_key=topsecret` strip works with the current `find("?")` because the path has no literal `?`).
- `test_strip_query_handles_encoded_question_mark` — PASS (`%3F` is three chars, none of which is `?`).
- `test_strip_query_drops_fragment` — **FAIL** (current code preserves `#frag` because `find("?")` doesn't see fragments).
- `test_strip_query_drops_userinfo` — **FAIL** (current code returns the URL unchanged because `find("?")` doesn't touch userinfo).

If the pass/fail split is different, **stop and investigate** — current behavior has drifted from what the spec assumed.

- [ ] **Step 3: Do not commit yet**

Task 4 implements the fix and the same tests then become a single passing class. Committing failing tests separately fragments the history. Keep the working tree dirty across Tasks 3 and 4.

---

### Task 4: `_strip_query` rewrite using `urlparse`

**Files:**
- Modify: `recost/_interceptor.py` (lines 76–84).

- [ ] **Step 1: Replace `_strip_query`**

Replace the current implementation:

```python
def _strip_query(url: str) -> str:
    """Strip query string from URL."""
    try:
        idx = url.find("?")
        if idx != -1:
            return url[:idx]
        return url
    except Exception:
        return url
```

with:

```python
def _strip_query(url: str) -> str:
    """Strip query string and fragment from URL, preserving scheme/netloc/path.

    Uses urlparse so encoded characters in the path (%3F, %23, etc.) and
    URL fragments cannot leak into event.url. The substring-based
    predecessor was correct for canonical URLs but fragile for any input
    where a `?` or `#` could appear in a non-query position, and it did
    not drop userinfo from the netloc.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            return url
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        path = parsed.path or ""
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url
```

`urlparse` is already imported at line 18 — no new imports needed.

- [ ] **Step 2: Run the privacy tests — all six must pass**

```bash
pytest tests/test_interceptor.py::TestPrivacy -v
```

Expected: all 6 pass.

- [ ] **Step 3: Run the full interceptor suite — no regressions**

```bash
pytest tests/test_interceptor.py -q
```

Expected: all tests pass, including the existing `test_strips_query_params` which still exercises the basic-strip case.

- [ ] **Step 4: Run mypy and ruff on the changed file**

```bash
mypy recost/
ruff check recost/ tests/
```

Expected: 0 errors from both.

- [ ] **Step 5: Commit both the test class and the fix together**

```bash
git add tests/test_interceptor.py recost/_interceptor.py
git commit -m "$(cat <<'EOF'
fix(interceptor): _strip_query uses urlparse — drops fragment + userinfo (#19)

The substring-based implementation correctly handled canonical URLs but
preserved fragments and userinfo, leaking either into event.url. The
urlparse-based rewrite drops both alongside the query string, and is
robust to encoded `%3F` / `%23` in the path.

Adds TestPrivacy (6 tests) pinning the contract:
- RawEvent has no header-shaped fields
- Authorization header values never appear in any captured field
- Query string secrets never appear in any captured field
- _strip_query handles %3F-in-path, drops #fragment, drops user:pass@

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `TestAiohttp` — 4 tests in `tests/test_interceptor.py`

Generic aiohttp interception coverage — Wave A added only body-sizing tests; these cover success/failure/exception/double-count.

**Files:**
- Modify: `tests/test_interceptor.py` — append a `TestAiohttp` class at the bottom (after `TestPrivacy`).

- [ ] **Step 1: Append the test class**

```python
# ---------------------------------------------------------------------------
# Generic aiohttp interception (#9)
# ---------------------------------------------------------------------------


class TestAiohttp:
    """Mirrors TestUrllib3 / TestHttpxAsync. Wave A's aiohttp tests cover only
    body sizing; these pin success, non-2xx, exception, and double-count."""

    @pytest.mark.asyncio
    async def test_captures_get(self, test_server):
        events: list[RawEvent] = []
        install(events.append)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{test_server}/aio") as resp:
                    assert resp.status == 200
                    await resp.read()
            assert len(events) == 1, f"expected 1 event, got {len(events)}"
            event = events[0]
            assert event.method == "GET"
            assert "/aio" in event.url
            assert event.status_code == 200
            assert event.latency_ms >= 0
        finally:
            uninstall()

    @pytest.mark.asyncio
    async def test_captures_non_2xx_marks_error(self, test_server):
        events: list[RawEvent] = []
        install(events.append)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{test_server}/notfound") as resp:
                    assert resp.status == 404
                    await resp.read()
            assert len(events) == 1
            assert events[0].status_code == 404
            assert events[0].error is True
        finally:
            uninstall()

    @pytest.mark.asyncio
    async def test_exception_path_captures_status_zero(self):
        """Pointing aiohttp at a closed port raises ClientError and we still
        capture an event with status_code=0, error=True."""
        events: list[RawEvent] = []
        install(events.append)
        try:
            import aiohttp
            dead_port = _find_free_port()  # very likely closed by the time we connect
            url = f"http://127.0.0.1:{dead_port}/x"
            with pytest.raises(aiohttp.ClientError):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        await resp.read()
            assert len(events) == 1
            assert events[0].status_code == 0
            assert events[0].error is True
        finally:
            uninstall()

    @pytest.mark.asyncio
    async def test_no_double_count_via_internal_connector(self, test_server):
        """One session.get must produce exactly one event, even though
        aiohttp's connector internals can use a DNS thread pool that hops
        OS threads. The dual task+thread reentrancy guard covers this."""
        events: list[RawEvent] = []
        install(events.append)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{test_server}/dc") as resp:
                    await resp.read()
            assert len(events) == 1, (
                f"expected 1 event, got {len(events)} — possible double-count "
                f"regression in the reentrancy guard"
            )
        finally:
            uninstall()
```

- [ ] **Step 2: Run the new class**

```bash
pytest tests/test_interceptor.py::TestAiohttp -v
```

Expected: all 4 pass. (No code change — these verify existing behavior.)

If `test_exception_path_captures_status_zero` fails because aiohttp raises a different exception subclass on this platform/version, widen the `pytest.raises` to `(aiohttp.ClientError, OSError)` and re-run. Do **not** widen further.

- [ ] **Step 3: Run the full interceptor suite**

```bash
pytest tests/test_interceptor.py -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_interceptor.py
git commit -m "$(cat <<'EOF'
test(interceptor): generic aiohttp coverage — success / 4xx / exception / no-double-count (#9)

Mirrors TestUrllib3 / TestHttpxAsync shape. Wave A added only body-sizing
tests; these pin the four invariants the README promises.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `TestSelfInstrumentation` — 1 test in `tests/test_transport.py`

Regression test for the README claim that the cloud transport uses `urllib.request` specifically so it doesn't get caught by its own urllib3 patch.

**Files:**
- Modify: `tests/test_transport.py` — append at the bottom.

- [ ] **Step 1: Append the test class**

```python
# ---------------------------------------------------------------------------
# Self-instrumentation (#9) — cloud transport must not trigger urllib3 patch
# ---------------------------------------------------------------------------


class TestSelfInstrumentation:
    """The cloud transport uses urllib.request (stdlib) so the interceptor's
    urllib3 patch never sees its own POSTs. If a future change starts using
    requests/httpx for the cloud POST, the SDK would loop on itself —
    every flush would generate one more event to flush."""

    def test_cloud_flush_does_not_trigger_urllib3_patch(self, cloud_server):
        from recost._interceptor import install, uninstall
        from recost._transport import _post_cloud
        events: list = []
        base_url, _ = cloud_server
        install(events.append)
        try:
            result = _post_cloud(
                url=f"{base_url}/projects/proj/telemetry",
                body='{"ping": 1}',
                api_key="rc-test",
                max_retries=0,
            )
            assert 200 <= result.status < 300, (
                f"cloud_server should return 202, got {result.status}"
            )
            assert events == [], (
                f"cloud transport leaked into interceptor — captured {len(events)} "
                f"event(s); the urllib.request-based path must not be patched"
            )
        finally:
            uninstall()
```

- [ ] **Step 2: Run the new test**

```bash
pytest tests/test_transport.py::TestSelfInstrumentation -v
```

Expected: PASS. The transport uses `urllib.request` (`_transport.py:81-91`) which is **not** patched (only `urllib3.HTTPConnectionPool.urlopen` is).

- [ ] **Step 3: Commit**

```bash
git add tests/test_transport.py
git commit -m "$(cat <<'EOF'
test(transport): cloud flush does not trigger the urllib3 patch (#9)

Pins the README claim that _post_cloud uses urllib.request specifically
to avoid self-instrumentation. A regression here would make every flush
generate another event to flush — silent telemetry amplification.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `Test5xxRetry` — 2 tests in `tests/test_transport.py`

Counterpart to the existing `test_no_retry_on_4xx`; verifies that 5xx responses retry exactly `max_retries` times with exponential backoff.

**Files:**
- Modify: `tests/test_transport.py` — append after `TestSelfInstrumentation`.

- [ ] **Step 1: Append the test class**

```python
# ---------------------------------------------------------------------------
# 5xx retry — exponential backoff, max_retries respected (#9)
# ---------------------------------------------------------------------------


class Test5xxRetry:
    """Counterpart to test_no_retry_on_4xx (line 121). 5xx is retriable;
    the loop in _post_cloud sleeps min(1.0 * 2**attempt, 10.0) between
    attempts and stops after max_retries + 1 total tries."""

    def test_5xx_retries_exactly_max_retries_attempts(self, cloud_server, monkeypatch):
        from recost._transport import Transport
        from recost._types import RecostConfig
        # Eliminate the real backoff so the test runs fast
        monkeypatch.setattr("recost._transport.time.sleep", lambda _s: None)
        base_url, _ = cloud_server
        _CloudHandler.response_code = 500
        config = RecostConfig(
            api_key="rc-test",
            project_id="proj-123",
            base_url=base_url,
            max_retries=2,
        )
        transport = Transport(config)
        transport.send(_make_summary())
        transport.dispose()
        # 1 initial attempt + 2 retries = 3 total
        assert len(_CloudHandler.received) == 3, (
            f"expected 3 total cloud POSTs (1 initial + 2 retries), got "
            f"{len(_CloudHandler.received)}"
        )

    def test_5xx_retry_uses_exponential_backoff(self, cloud_server, monkeypatch):
        from recost._transport import Transport
        from recost._types import RecostConfig
        sleeps: list[float] = []
        monkeypatch.setattr(
            "recost._transport.time.sleep",
            lambda s: sleeps.append(s),
        )
        base_url, _ = cloud_server
        _CloudHandler.response_code = 500
        config = RecostConfig(
            api_key="rc-test",
            project_id="proj-123",
            base_url=base_url,
            max_retries=3,
        )
        transport = Transport(config)
        transport.send(_make_summary())
        transport.dispose()
        # _post_cloud sleeps between attempts when attempt < max_retries.
        # For max_retries=3 → attempts 0,1,2 each sleep; attempt 3 doesn't.
        # Shape: min(1.0 * 2**attempt, 10.0) → [1.0, 2.0, 4.0].
        assert sleeps == pytest.approx([1.0, 2.0, 4.0]), (
            f"expected exponential backoff [1.0, 2.0, 4.0], got {sleeps}"
        )
```

- [ ] **Step 2: Run the new class**

```bash
pytest tests/test_transport.py::Test5xxRetry -v
```

Expected: both pass.

- [ ] **Step 3: Run the full transport suite — no regressions**

```bash
pytest tests/test_transport.py -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_transport.py
git commit -m "$(cat <<'EOF'
test(transport): 5xx retry — max_retries + exponential backoff (#9)

Counterpart to test_no_retry_on_4xx. Verifies that 500s retry max_retries
times (so max_retries+1 total POSTs), and that the sleep durations match
the documented min(1.0 * 2**attempt, 10.0) schedule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Three small-gap tests across `test_init.py` and `test_aggregator.py`

Three independent tests packaged as one task because each is ~15 lines.

**Files:**
- Modify: `tests/test_init.py` — append `test_legacy_flush_interval_emits_deprecation_warning` and `test_dispose_prevents_further_flushes`.
- Modify: `tests/test_aggregator.py` — append `test_overflow_triggers_early_flush`.

- [ ] **Step 1: Append the deprecation-warning test to `tests/test_init.py`**

```python
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
```

- [ ] **Step 2: Append the dispose-stops-flushes test to `tests/test_init.py`**

```python
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
        auto_shutdown_handlers=False,  # avoid the atexit path counting toward this test
    ))

    # Generate one event so the aggregator has something to flush
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

    # Let at least one periodic flush fire
    _time.sleep(0.5)
    pre_dispose = send_count[0]

    handle.dispose()
    send_count[0] = 0
    _time.sleep(0.6)

    assert send_count[0] == 0, (
        f"send was called {send_count[0]} times after dispose() — "
        f"timer was {pre_dispose} pre-dispose; dispose did not stop the loop"
    )
```

- [ ] **Step 3: Append the would_overflow test to `tests/test_aggregator.py`**

```python
def test_overflow_triggers_early_flush(monkeypatch) -> None:
    """When ingesting an event would push the aggregator past max_buckets,
    the on_event closure in init() flushes the current window first so
    that the new event lands in a fresh window. Verified by counting
    Transport.send invocations."""
    from recost import init, RecostConfig
    from recost._transport import Transport
    from recost._interceptor import _callback
    from recost._types import RawEvent

    sent_summaries: list = []
    real_send = Transport.send

    def _capturing_send(self, summary):
        sent_summaries.append(summary)
        return real_send(self, summary)

    monkeypatch.setattr(Transport, "send", _capturing_send)

    handle = init(RecostConfig(
        enabled=True,
        api_key="rc-test",
        project_id="p",
        base_url="http://127.0.0.1:1",
        max_buckets=2,
        # Long flush interval so the timer can't fire during the test
        flush_interval_ms=600_000,
        auto_shutdown_handlers=False,
    ))
    try:
        assert _callback is not None

        def _event(host: str, path: str, method: str) -> RawEvent:
            return RawEvent(
                timestamp="2026-05-13T00:00:00Z",
                method=method,
                url=f"https://{host}{path}",
                host=host,
                path=path,
                status_code=200,
                latency_ms=10,
                request_bytes=0,
                response_bytes=0,
            )

        # Three distinct (provider, endpoint, method) triplets so each is its
        # own bucket. With max_buckets=2, ingesting the 3rd must trigger
        # an early flush before insertion.
        _callback(_event("api.openai.com", "/v1/chat/completions", "POST"))
        _callback(_event("api.openai.com", "/v1/embeddings", "POST"))
        # 3rd unique triplet — would push past max_buckets=2 → early flush
        _callback(_event("api.openai.com", "/v1/models", "GET"))

        assert len(sent_summaries) == 1, (
            f"expected exactly one early flush from would_overflow, "
            f"got {len(sent_summaries)}"
        )
        flushed = sent_summaries[0]
        # The flushed window held the first two triplets; the 3rd is now
        # in a fresh window inside the aggregator (not yet flushed).
        assert len(flushed.metrics) == 2
    finally:
        handle.dispose()
```

- [ ] **Step 4: Run each new test**

```bash
pytest tests/test_init.py::test_legacy_flush_interval_emits_deprecation_warning -v
pytest tests/test_init.py::test_dispose_prevents_further_flushes -v
pytest tests/test_aggregator.py::test_overflow_triggers_early_flush -v
```

Expected: all three pass.

- [ ] **Step 5: Run the full suites for those files — no regressions**

```bash
pytest tests/test_init.py tests/test_aggregator.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_init.py tests/test_aggregator.py
git commit -m "$(cat <<'EOF'
test: small gap coverage — deprecation, overflow, dispose stops flushes (#9)

- test_legacy_flush_interval_emits_deprecation_warning — verifies the
  legacy seconds-based flush_interval still works and emits the documented
  DeprecationWarning.
- test_dispose_prevents_further_flushes — guards the dispose path against
  regression where the timer or atexit hook would keep firing post-dispose.
- test_overflow_triggers_early_flush — pins the on_event would_overflow
  branch that protects the API from a >max_buckets payload.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Final PR-A sanity + push + open PR

**Files:** none modified — pure verification + git/gh operations.

- [ ] **Step 1: Run the full test suite locally**

```bash
pytest -q
```

Expected: every test passes on Python 3.14 (current local). 16 new tests added across this PR.

- [ ] **Step 2: Run mypy strict**

```bash
mypy recost/
```

Expected: 0 errors.

- [ ] **Step 3: Run ruff on both source and tests**

```bash
ruff check recost/ tests/
```

Expected: 0 errors. If a ruff error fires on the new code, fix it inline — usually a missing blank line or an unused import.

- [ ] **Step 4: Verify branch state and push**

```bash
git branch --show-current
git status -uno
git log --oneline origin/main..HEAD
git push -u origin wave-b1/pr-a-test-fortification
```

Expected: branch is `wave-b1/pr-a-test-fortification`; commits visible are exactly the ones from Tasks 2–8 (5 commits — Tasks 3+4 share a commit). No uncommitted production code.

- [ ] **Step 5: Open PR-A**

```bash
gh pr create \
  --base main \
  --head wave-b1/pr-a-test-fortification \
  --title "Wave B-1 PR-A: test fortification + privacy fix" \
  --body "$(cat <<'EOF'
## Summary

- Closes #19. `_strip_query` rewritten to use `urllib.parse.urlparse` —
  drops fragment + userinfo alongside the query string.
- 16 new tests across `test_interceptor.py`, `test_transport.py`,
  `test_init.py`, `test_aggregator.py`. Closes most of #9 (the remaining
  two missing-dep tests land in PR-B).
- Audit-confirmed #11 close: the `_UNSET` sentinel is in place
  (`_interceptor.py:48`) and the dead `pool_connections` /
  `pool_maxsize` kwargs are gone. Close after PR-B merges.

### Tests added

- `TestPrivacy` (6) — header non-capture, query non-leak, `_strip_query`
  unit cases (encoded `%3F`, fragment, userinfo).
- `TestAiohttp` (4) — success, non-2xx, exception path, no-double-count.
- `TestSelfInstrumentation` (1) — cloud flush does not trigger urllib3
  patch.
- `Test5xxRetry` (2) — exact attempt count, exponential backoff schedule.
- `test_legacy_flush_interval_emits_deprecation_warning`.
- `test_dispose_prevents_further_flushes`.
- `test_overflow_triggers_early_flush`.

### Code change

`recost/_interceptor.py::_strip_query` — one function rewritten.
`tests/test_interceptor.py::_Handler.do_GET` — extended for `/notfound`.

## Test plan

- [ ] CI green on Python 3.9, 3.10, 3.11, 3.12.
- [ ] `mypy recost/` clean.
- [ ] `ruff check recost/ tests/` clean.
- [ ] Manual: `pytest tests/test_interceptor.py::TestPrivacy -v` confirms
      the 6 privacy contracts.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## PR-B — Missing-dep tests (separate PR, off `origin/main`)

PR-B branch: `wave-b1/pr-b-missing-dep-tests`, off `origin/main` (**not** off PR-A).

### Task 10: Branch off `origin/main` for PR-B

**Files:** none yet.

- [ ] **Step 1: Branch off `origin/main` — do NOT chain off PR-A**

```bash
git fetch origin
git checkout -b wave-b1/pr-b-missing-dep-tests origin/main
```

Expected: switched to a clean branch starting from `origin/main`. `git log --oneline -3` should NOT contain any of PR-A's commits.

If you accidentally branched off PR-A, fix it before adding tests:

```bash
git checkout main && git pull && git checkout -b wave-b1/pr-b-missing-dep-tests
```

---

### Task 11: `test_local_transport_no_op_without_websockets`

The `sys.modules` patching pattern is fiddly — read the steps in full before starting.

**Files:**
- Modify: `tests/test_transport.py` — append at the bottom.

- [ ] **Step 1: Append the test**

```python
# ---------------------------------------------------------------------------
# Graceful degradation — websockets missing (#9)
# ---------------------------------------------------------------------------


def test_local_transport_no_op_without_websockets(monkeypatch) -> None:
    """When the optional `websockets` dependency is not installed, the
    `_LocalTransport` constructor must:
      - construct without raising,
      - log a warning telling the user how to install it,
      - mark `_has_websockets = False` so send()/dispose() short-circuit.

    We fake the missing import via sys.modules + importlib.reload, then
    reload again with the real module after the test so subsequent tests
    in the run see the truth.
    """
    import importlib
    import sys
    import time as _time

    monkeypatch.setitem(sys.modules, "websockets", None)
    import recost._transport as transport_mod
    importlib.reload(transport_mod)
    try:
        lt = transport_mod._LocalTransport(port=12345)
        assert lt._has_websockets is False, (
            "expected _has_websockets=False when websockets is unavailable"
        )
        # send() and dispose() must not raise — they are no-ops
        lt.send("payload")
        start = _time.monotonic()
        lt.dispose()
        assert _time.monotonic() - start < 0.5, (
            "dispose() blocked unexpectedly when websockets was missing"
        )
    finally:
        # Restore the real module so subsequent tests aren't poisoned
        monkeypatch.undo()
        importlib.reload(transport_mod)
```

- [ ] **Step 2: Run the test in isolation**

```bash
pytest tests/test_transport.py::test_local_transport_no_op_without_websockets -v
```

Expected: PASS. If it fails because `websockets` is currently installed and the reload still picks it up, double-check that `monkeypatch.setitem(sys.modules, "websockets", None)` fires **before** the `importlib.reload(transport_mod)` call.

- [ ] **Step 3: Run the FULL transport suite — verify no poisoning**

```bash
pytest tests/test_transport.py -q
```

Expected: all tests pass. If a subsequent test fails because `websockets` is None or because `_LocalTransport._has_websockets` is False, the cleanup `monkeypatch.undo()` + reload-back didn't run — investigate and fix the teardown.

- [ ] **Step 4: Commit**

```bash
git add tests/test_transport.py
git commit -m "$(cat <<'EOF'
test(transport): _LocalTransport graceful no-op when websockets missing (#9)

Fakes the missing optional dependency via sys.modules + importlib.reload
and verifies the constructor, send, and dispose all degrade gracefully —
no exception, prompt return.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `test_flask_extension_raises_clear_error_without_flask`

Same pattern as Task 11 but for the Flask adapter.

**Files:**
- Modify: `tests/test_flask.py` — append at the bottom.

- [ ] **Step 1: Append the test**

```python
def test_flask_extension_raises_clear_error_without_flask(monkeypatch) -> None:
    """When `flask` is not installed, importing
    `recost.frameworks.flask.RecostExtension` yields the stub class whose
    constructor raises ImportError with an install hint.

    We fake the missing import via sys.modules + importlib.reload, then
    reload again with the real module after the test so subsequent tests
    aren't poisoned.
    """
    import importlib
    import sys
    import pytest as _pytest

    monkeypatch.setitem(sys.modules, "flask", None)
    import recost.frameworks.flask as flask_mod
    importlib.reload(flask_mod)
    try:
        with _pytest.raises(ImportError, match="pip install recost\\[flask\\]"):
            flask_mod.RecostExtension()
    finally:
        monkeypatch.undo()
        importlib.reload(flask_mod)
```

- [ ] **Step 2: Run the test in isolation**

```bash
pytest tests/test_flask.py::test_flask_extension_raises_clear_error_without_flask -v
```

Expected: PASS.

- [ ] **Step 3: Run the FULL Flask suite — verify no poisoning**

```bash
pytest tests/test_flask.py -q
```

Expected: all existing Flask tests still pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_flask.py
git commit -m "$(cat <<'EOF'
test(flask): RecostExtension raises clear ImportError when flask is missing (#9)

Fakes the missing optional dependency via sys.modules + importlib.reload
and verifies the stub class raises ImportError with the documented
install hint.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Final PR-B sanity + push + open PR

- [ ] **Step 1: Run the full test suite**

```bash
pytest -q
```

Expected: every test passes. 2 new tests added across this PR.

- [ ] **Step 2: mypy + ruff**

```bash
mypy recost/
ruff check recost/ tests/
```

Expected: 0 errors from both.

- [ ] **Step 3: Verify branch state and push**

```bash
git branch --show-current
git log --oneline origin/main..HEAD
git push -u origin wave-b1/pr-b-missing-dep-tests
```

Expected: branch is `wave-b1/pr-b-missing-dep-tests`; exactly 2 commits visible.

- [ ] **Step 4: Open PR-B**

```bash
gh pr create \
  --base main \
  --head wave-b1/pr-b-missing-dep-tests \
  --title "Wave B-1 PR-B: missing-dep test coverage" \
  --body "$(cat <<'EOF'
## Summary

Closes #9 (the remaining two missing-dep tests not covered by PR-A).

- `test_local_transport_no_op_without_websockets` — verifies
  `_LocalTransport` constructs, sends, and disposes gracefully when the
  optional `websockets` dependency is absent.
- `test_flask_extension_raises_clear_error_without_flask` — verifies
  the stub `RecostExtension` raises `ImportError` with the
  `pip install recost[flask]` hint.

### Pattern

Both tests use `monkeypatch.setitem(sys.modules, "<name>", None)` plus
`importlib.reload(...)` to fake the missing import, then
`monkeypatch.undo()` + reload-back in `finally` to restore the real
module so subsequent tests in the run aren't poisoned. Isolated as PR-B
because the test shape differs meaningfully from everything in PR-A.

## Test plan

- [ ] CI green on Python 3.9, 3.10, 3.11, 3.12.
- [ ] `pytest tests/test_transport.py -q` clean (verifies no poisoning).
- [ ] `pytest tests/test_flask.py -q` clean (verifies no poisoning).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

### Task 14: Close #11

After both PRs merge, close `#11` with an audit comment.

- [ ] **Step 1: Post the audit comment and close**

```bash
gh issue comment 11 --body "$(cat <<'EOF'
Audit confirms the remaining ask is functionally complete on the code
side:

- `_UNSET` sentinel in place at `recost/_interceptor.py:48`.
- Dead `pool_connections` / `pool_maxsize` kwargs removed from the
  urllib3 patch signature.

No further code change required. Closing.
EOF
)" && gh issue close 11
```

Expected: comment posted, issue closed.

---

## Acceptance checklist

After both PRs merge:

- [ ] `pytest -q` passes on `origin/main` on Python 3.9, 3.10, 3.11, 3.12 in CI.
- [ ] `mypy recost/` returns 0 errors in CI.
- [ ] `ruff check recost/ tests/` returns 0 errors in CI.
- [ ] `recost/_interceptor.py::_strip_query` uses `urlparse`.
- [ ] All 6 `TestPrivacy` tests present and green.
- [ ] All 4 `TestAiohttp` tests present and green.
- [ ] `TestSelfInstrumentation` (1) and `Test5xxRetry` (2) present and green.
- [ ] `test_legacy_flush_interval_emits_deprecation_warning`,
      `test_dispose_prevents_further_flushes`,
      `test_overflow_triggers_early_flush` present and green.
- [ ] `test_local_transport_no_op_without_websockets` and
      `test_flask_extension_raises_clear_error_without_flask` present
      and green; subsequent tests in the same `pytest` run unaffected
      (no `sys.modules` poisoning).
- [ ] #19 closed by PR-A; #9 closed by PR-B; #11 closed by audit comment.

---

## Safety rails (carried over from Wave A constraints)

- **Always `git branch --show-current` before committing.** Both PR
  branches must be off `origin/main`, not off each other.
- **Never `git checkout <SHA>`.** Only branch names.
- **Never `git reset --hard`, `git rebase`, or `git push --force` on
  shared branches.** During Wave A an implementer detached HEAD and lost
  3 commits.
- **Never `git commit --no-verify` or `--no-gpg-sign` unless asked.**
- **If a pre-commit hook fails, the commit did not happen** — fix the
  issue, re-stage, and create a NEW commit (do not `--amend`).
- **Reuse existing fixtures.** `mock_http_server_200`, `cloud_server`,
  module-scoped `test_server` already exist. Don't duplicate.
- **No new `conftest.py` fixtures.** Everything new fits in the test
  file that uses it.
- **Test the typed-error API, not README copy.** `isinstance(e,
  RecostAuthError)`, not `"401" in str(e)`. Wave B-1 doesn't add any new
  typed errors but inherits this rule.
