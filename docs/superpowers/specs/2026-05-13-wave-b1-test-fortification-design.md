# Wave B-1 — Test Fortification & Privacy Hardening Design

**Date:** 2026-05-13
**Status:** Approved for planning
**Scope:** `middleware-python` (this repo only)
**Tracking issues:** #9, #19, #11 (audit close-out)

## Goal

Close the test-coverage gap that Wave A's README claims rest on, and harden
`_strip_query` so the privacy contract is enforced in code, not just in
convention. After Wave B-1, every README-promised behavior — aiohttp
interception, privacy of headers and query strings, no transport
self-instrumentation, retry on 5xx — has at least one direct test, and the
URL stripping logic uses `urllib.parse.urlparse` instead of substring
slicing.

## Non-goals

- New behavioral changes (new error types, new transport branches, fork-safety
  changes). Those are Wave A territory and already shipped.
- Refactoring `_interceptor.py` or `_transport.py` for clarity or modularity.
- CI coverage instrumentation / coverage gating.
- README rewrites beyond a one-line adjustment if the `_strip_query` change
  makes a previously-accurate sentence subtly inaccurate (audit confirmed:
  no such sentence exists today).
- Local-WebSocket protocol or reconnect hardening (#7).
- Node-parity items beyond #19 (which is the Python equivalent of #19; the
  Node version stays out).

## Public API changes

**None.** Wave B-1 is test-only plus one internal helper rewrite. `RawEvent`,
`RecostHandle`, `RecostConfig`, and the error hierarchy stay exactly as Wave
A left them.

## Code change — `_strip_query` (#19)

In `recost/_interceptor.py:76-84`, replace the substring-based implementation
with `urllib.parse.urlparse`:

```python
def _strip_query(url: str) -> str:
    """Strip query string and fragment from URL, preserving scheme/netloc/path.

    Uses urlparse so encoded characters in the path (%3F, %23, etc.) and
    URL fragments cannot leak into event.url. The original substring-based
    implementation was correct for canonical URLs but fragile for any input
    where a `?` or `#` could appear in a non-query position, and it did not
    drop userinfo from the netloc.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            return url  # malformed — preserve input rather than fabricate
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        path = parsed.path or ""
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url
```

**Behavioral notes:**

- **userinfo dropped.** `parsed.hostname` does not include `user:pass@`.
  Credentials in URL userinfo are now stripped from `event.url`.
- **fragment dropped.** `parsed.fragment` is discarded alongside
  `parsed.query`.
- **port preserved when present in the parsed URL.** `parsed.port`
  returns whatever the URL string carries — `None` if absent, else the
  integer. We do not special-case default ports (80/443). The urllib3
  patch's `_patched_urlopen` already drops 80/443 before constructing
  the URL (`_interceptor.py:151-152`); URLs coming from httpx and aiohttp
  typically already omit default ports, so this is rarely visible in
  practice. If a caller passes `https://h:443/p` explicitly,
  `_strip_query` returns it as `https://h:443/p` — same as today's
  substring implementation.
- **malformed-input fallback.** If `urlparse` returns empty scheme + empty
  netloc (e.g., a bare path), the input is returned as-is. `_build_event`
  already tolerates an empty host/path.
- **Double-parsing cost.** `_build_event` re-parses the cleaned URL via
  `urlparse` to extract `host` and `path`. Re-parsing twice per intercepted
  request is negligible (~µs) and keeps the responsibility boundary clear.

`#11` audit: the `_UNSET` sentinel is in place (`_interceptor.py:48`) and the
dead `pool_connections` / `pool_maxsize` kwargs are gone. The remaining
audit ask is functionally complete; close the issue via PR-A's description
or a follow-up comment.

## Test plan

Test placement follows the existing convention — same file as the module
under test. Two PRs, structure described in **Sequencing** below.

### Privacy contract — `tests/test_interceptor.py`

New `TestPrivacy` class. Six tests:

1. `test_raw_event_has_no_headers_field` — static check via
   `dataclasses.fields(RawEvent)`. Asserts no field named `headers` or
   `request_headers` or `response_headers` exists. Guards against accidental
   future addition.
2. `test_authorization_header_never_leaves_a_trace` — `requests.get(url,
   headers={"Authorization": "Bearer secret123"})` against
   `mock_http_server_200`. Iterates `dataclasses.asdict(event)` and asserts
   `"secret123"` does not appear in any value.
3. `test_query_string_never_leaves_a_trace` — `requests.get(url +
   "?api_key=topsecret")`. Same `asdict` scan: `"topsecret"` appears nowhere.
4. `test_strip_query_handles_encoded_question_mark` — direct unit test of
   `_strip_query("https://api.example.com/p%3Fath?real=secret")` →
   `"https://api.example.com/p%3Fath"`. Asserts `"secret"` is not in the
   result.
5. `test_strip_query_drops_fragment` — `_strip_query("https://h/p#frag")`
   → `"https://h/p"`.
6. `test_strip_query_drops_userinfo` —
   `_strip_query("https://user:pass@h/p")` → `"https://h/p"`. Asserts
   neither `"user"` nor `"pass"` appears in the result.

### Generic aiohttp coverage — `tests/test_interceptor.py`

New `TestAiohttp` class, four tests. All async (`@pytest.mark.asyncio`),
all use the existing module-scoped `test_server` fixture. Extend the
existing `_Handler` to return 404 for `path == "/notfound"`:

1. `test_captures_get` — `aiohttp.ClientSession.get(f"{test_server}/aio")`.
   Asserts `event.method == "GET"`, `event.url` contains `/aio`,
   `event.status_code == 200`, `event.latency_ms >= 0`. Exactly one event
   captured.
2. `test_captures_non_2xx_marks_error` — request to `/notfound`. Asserts
   `event.status_code == 404` and `event.error is True`.
3. `test_exception_path_captures_status_zero` — point aiohttp at a closed
   port (use a local `_find_free_port()` helper, immediately released).
   Asserts (a) the exception propagates out of `session.get()`, (b) the
   captured event has `status_code == 0` and `error is True`. Match
   `aiohttp.ClientError` (the parent class) rather than the specific
   subclass so the test survives aiohttp version drift.
4. `test_no_double_count_via_internal_connector` — issue one
   `session.get()` from inside the async loop. Asserts exactly **one**
   event captured. Reentrancy guard already covers `asyncio.to_thread` —
   this test pins the invariant for aiohttp's own connector thread pool
   usage so future patch changes can't regress it.

### Self-instrumentation — `tests/test_transport.py`

New `TestSelfInstrumentation` class, one test:

- `test_cloud_flush_does_not_trigger_urllib3_patch` — install the
  interceptor with a capturing callback. Run a cloud-mode flush via
  `Transport.send()` against the `cloud_server` fixture (or call
  `_post_cloud` directly for tighter scope). Assert **zero** events were
  recorded by the interceptor callback during the flush. Regression test
  for the README claim that the cloud transport uses `urllib.request`
  specifically to avoid self-instrumentation.

### 5xx retry — `tests/test_transport.py`

New `Test5xxRetry` class, two tests:

- `test_5xx_retries_exactly_max_retries_attempts` — `_CloudHandler.response_code
  = 500`. Configure `max_retries=2`. Send one summary. Assert the server
  received exactly **3** requests (1 initial + 2 retries). Counterpart to
  the existing `test_no_retry_on_4xx` (`tests/test_transport.py:121-134`).
- `test_5xx_retry_uses_exponential_backoff` — monkeypatch
  `recost._transport.time.sleep` to record sleep durations. Configure
  `max_retries=3`. Assert the recorded sleeps match the
  `min(1.0 * (2 ** attempt), 10.0)` shape from `_transport.py:115`:
  `[1.0, 2.0, 4.0]`. (Use `pytest.approx` since `time.sleep` is the only
  external dependency we care to verify.)

### Deprecation warning — `tests/test_init.py`

One test:

- `test_legacy_flush_interval_emits_deprecation_warning` — `with
  pytest.warns(DeprecationWarning, match="flush_interval is deprecated")`.
  Calls `init(RecostConfig(flush_interval=1.0, enabled=True))`. Verifies
  the warning fires and the timer thread is alive (so the legacy value was
  still honored). Disposes the handle.

### `would_overflow` early-flush — `tests/test_aggregator.py`

One test (placed in test_aggregator.py since the early-flush behavior is
the aggregator's contract, even though `on_event` is the caller):

- `test_overflow_triggers_early_flush` — init with `max_buckets=2` and
  `flush_interval_ms=600_000` so the timer cannot fire. Monkeypatch
  `Transport.send` to count calls and capture sent summaries. Push three
  distinct (provider, endpoint, method) events through the interceptor
  callback. Assert `Transport.send` was called exactly once (the early
  flush when the 3rd triplet would have crossed `max_buckets=2`) and that
  the post-overflow event lives in a fresh window.

### `dispose` stops flushes — `tests/test_init.py`

One test:

- `test_dispose_prevents_further_flushes` — init with
  `flush_interval_ms=200` and a `Transport.send` patched to count calls.
  Wait ~500ms (so at least one flush has fired). Call `handle.dispose()`.
  Reset the counter. Sleep another ~600ms. Assert the counter stays at 0 —
  no flush after dispose. Also guards the `_atexit_callback` unregistration
  path (`_init.py:88-93`) from regression.

### Missing-dep tests — PR-B, separate PR

Two tests, both use `monkeypatch.setitem(sys.modules, ..., None)` plus
`importlib.reload` to fake a missing optional dep:

- `tests/test_transport.py::test_local_transport_no_op_without_websockets` —
  fake `sys.modules["websockets"] = None`, reload `recost._transport`,
  instantiate `_LocalTransport(port=...)`. Assert `_has_websockets is
  False`, that `send()` does not raise, that `dispose()` returns under
  100ms. Tear down with `monkeypatch.undo()` + reload-back so subsequent
  tests in the run see the real `websockets` module.
- `tests/test_flask.py::test_flask_extension_raises_clear_error_without_flask`
  — fake `sys.modules["flask"] = None`, reload `recost.frameworks.flask`,
  instantiate the stub `RecostExtension(...)`. Assert it raises
  `ImportError` with the install hint. Same tear-down pattern.

## Test infrastructure

**Reused as-is:**

- `mock_http_server_200` (`conftest.py`) — privacy contract tests, aiohttp
  success path, `_strip_query` regression tests (no HTTP needed for those —
  direct calls).
- `cloud_server` (`test_transport.py`, mutable `_CloudHandler.response_code`,
  body capture) — self-instrumentation, 5xx retry, 5xx-backoff.
- Module-scoped `test_server` (`test_interceptor.py`) — `TestAiohttp` suite.

**Minimal additions:**

- Extend `_Handler` in `test_interceptor.py` to return `404` when
  `self.path == "/notfound"` — single branch in `do_GET`.
- Inline `_find_free_port()` helper in `test_interceptor.py` for the
  aiohttp exception-path test (same 3-line shape as
  `tests/test_transport.py:261`).
- No `conftest.py` additions. Existing fixtures cover the new tests.

## Sequencing — two PRs

Both PRs branch off the current `origin/main`. Neither chains off the
other.

### PR-A — Test fortification + privacy fix

- `#19` fix: `_strip_query` → `urlparse`.
- `TestPrivacy` (6 tests), `TestAiohttp` (4 tests), `TestSelfInstrumentation`
  (1 test), `Test5xxRetry` (2 tests), the three non-missing-dep small-gap
  tests (deprecation, would_overflow, dispose-stops-flushes).
- PR description: `Closes #19.` (`#9` stays open — PR-B closes it.)
  Reference `#11` audit confirmation in the body (close after merge if no
  separate commit is needed).

**Total: 16 new tests + 1 code change + 1 test-handler tweak.**

### PR-B — Missing-dep tests

- `test_local_transport_no_op_without_websockets`.
- `test_flask_extension_raises_clear_error_without_flask`.

**Total: 2 new tests, no production code changes.**

PR-B description: `Closes #9.`

PR-B is split out because its test shape (`sys.modules` patch +
`importlib.reload`) differs meaningfully from everything else in PR-A and
requires extra care to avoid poisoning sibling tests. Keeping it isolated
limits the blast radius of a missed `monkeypatch.undo()`.

## Acceptance criteria

Wave B-1 is done when, on a clean checkout of `origin/main` after both PRs
merge:

- All eight test groups described in the **Test plan** are present and
  green on Python 3.9, 3.10, 3.11, 3.12 (CI matrix).
- `recost/_interceptor.py::_strip_query` uses `urllib.parse.urlparse`.
- `mypy recost/` returns 0 errors.
- `ruff check recost/ tests/` returns 0 errors.
- `pytest` passes — full suite, no skips beyond the existing
  `pytest.importorskip("websockets")` markers.
- PR-A description includes `Closes #19`. PR-B description includes
  `Closes #9`.
- `#11` is closed (via PR-A's body or a separate comment confirming the
  audit).
- README is unchanged (audit confirmed no claim becomes inaccurate from
  the `_strip_query` switch).

## Risks and mitigations

- **aiohttp version drift on the exception path.** Different aiohttp
  releases raise different `ClientError` subclasses for a closed port.
  Mitigate by asserting `pytest.raises(aiohttp.ClientError)` (the parent
  class) rather than `ClientConnectorError` specifically.
- **`sys.modules` poisoning across tests.** A missed `monkeypatch.undo()`
  or skipped reload-back would make subsequent tests see a stubbed
  `websockets` or `flask`. Mitigate with strict per-test teardown in
  `finally` blocks and one explicit reload-back after `monkeypatch.undo()`.
- **`time.sleep` monkeypatch hiding a real timing bug.** Confine the patch
  to the `test_5xx_retry_uses_exponential_backoff` test only via
  function-scoped `monkeypatch.setattr` (auto-restored at test exit).
- **Aggregator overflow test depending on private state.** Use only
  `Transport.send` patching and event-callback paths. Do not poke
  `aggregator._buckets` or other internals.
- **`would_overflow` test sensitivity to event-callback ordering.**
  Trigger events synchronously via `recost._interceptor._callback(...)` so
  the test does not depend on real HTTP timing.
- **PR-B reload poisoning leaking into PR-A in a fresh checkout.** Both
  PRs are off `origin/main` and merged independently — they cannot share
  test state. Risk only matters within a single test run; mitigated by
  per-test teardown.

## Out of scope (for future waves)

- Coverage instrumentation, mutation testing, hypothesis fuzzing.
- Per-provider behavioral tests (already covered in
  `test_provider_registry.py`).
- Local-WebSocket protocol hardening (#7) — needs coordinated
  extension-side work.
- Exclude-pattern semantics rework (#12).
- Node-parity items (#16, #20, #21, #22, #23) — Wave C territory.
