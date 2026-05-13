# `recost` (Python) — Audit Findings

Date: 2026-05-13
Audit scope: full SDK — tests, lint, mypy, source code, docs vs. reality, runtime behavior.

## State of the build

| Check | Result |
|---|---|
| `pytest` | 129 / 129 passing (~43s) |
| `ruff check recost/` | 1 error (unused import) |
| `mypy recost/` (strict) | **35 errors across 6 files** — README claims strict-clean. |
| `python -c "from recost import init"` | OK |

Functionally the SDK runs; the mypy posture is broken, there's a real thread-safety bug in the aggregator, fork-safety is absent (breaking prefork servers), and naming is fragmented (`Recost` / `ReCost` / `EcoAPI*` are all referenced somewhere). The 13 consolidated issues below collect 25 original findings into the smallest set of focused, independently-fixable PRs.

Priorities: **P0** = correctness / shipping bug, **P1** = should fix before next release, **P2** = polish.

---

## Consolidated GitHub issues to file

---

### 1. `Aggregator` is not thread-safe — flush vs. ingest race — P0

**Body:**

`recost/_aggregator.py:127-132`'s `flush()` iterates `self._buckets.values()` while user threads concurrently call `ingest()` (via interceptor wrappers, which run on whatever thread issued the HTTP request). The flush is driven by a background `threading.Timer`. Concurrent dict mutation during iteration raises `RuntimeError: dictionary changed size during iteration`.

Real race under any non-trivial load. There is no lock anywhere in `Aggregator`.

**Fix:** wrap `ingest`, `flush`, and `would_overflow` with a `threading.RLock`. Add a regression test that pumps N worker threads into `ingest` while the timer thread calls `flush`.

**Files:** `recost/_aggregator.py`, `tests/test_aggregator.py`

**Includes:** original #1.

---

### 2. 35 mypy strict errors — README claims strict-clean — P0

**Body:**

`README.md` and `CLAUDE.md` both advertise `mypy --strict` cleanliness. `mypy recost/` reports 35 errors across 6 files. Highlights: `_interceptor.py`'s `latency_ms: int` parameter receives a `float`; `frameworks/flask.py:33`'s `self._handle = None` is inferred as `None` and breaks reassignment; stale `# type: ignore` comments flagged as unused.

**Fix:** decide on the actual mypy contract. Either run mypy in CI and fix the 35 errors, or drop the strict-clean claim from the README. The first option is correct.

**Files:** `recost/_interceptor.py`, `recost/frameworks/flask.py`, others; `README.md`; `pyproject.toml`; CI workflow.

**Includes:** original #2.

---

### 3. No fork-safety — `gunicorn` / `Celery` prefork workers report nothing — P0

**Body:**

`recost/_init.py` registers no `os.register_at_fork` hooks. After a fork:
- Patched method-object references are inherited (fine — class attrs).
- The module-level `_handle` global points at the parent's `RecostHandle`, but **no timer thread is running in the child**. Flushes never fire.
- `Transport._local._loop` references the parent's asyncio loop, which doesn't exist in the child. `run_coroutine_threadsafe` raises and is swallowed (`_transport.py:195-197`). All events queue silently and never send.

Symptom: every gunicorn pre-fork or Celery worker started after `init()` collects metrics into the void. Most production Python deployments use prefork servers — this is the single largest production gap.

**Fix:** register `os.register_at_fork(after_in_child=_reinit_after_fork)` that re-creates the timer thread and transport thread in the child. Alternatively, document a "call `init()` in the worker's `post_fork` hook" pattern and refuse to instrument until `init()` has been called in the current PID.

**Files:** `recost/_init.py`, `recost/_transport.py`, `README.md`, `tests/test_init.py`

**Includes:** original #16.

---

### 4. Module-level state races: `_handle`, `install`/`uninstall`, init-vs-dispose — P1

**Body:**

Multiple module globals are read/written without locks:

1. `_init.py:78, 225` reads/writes `_handle` without a lock. Two threads racing `init()` can both pass the `if _handle is not None` guard, both call `install()`, and orphan the first thread's transport + aggregator (the background timer thread leaks).

2. Independent of (1), the *patches and the callback registration* can desynchronize. Thread A in `dispose()` is mid-`_unpatch_*` while Thread B in `init()` calls `install(on_event)` and sees `_installed=True` (set by A but not yet cleared). B short-circuits — the new init has no patches.

**Fix:** guard `init()`, `dispose()`, `install()`, and `uninstall()` with a single module-level `threading.RLock`. Add tests that race both pairs.

**Files:** `recost/_init.py`, `recost/_interceptor.py`, `tests/test_init.py`

**Includes:** original #3, #20.

---

### 5. Naming chaos and stale docs across README and CLAUDE.md — P1

**Body:**

Brand name and class names are used inconsistently:

| Where | Spelling |
|---|---|
| `README.md:1, 3` | `Recost` |
| `README.md` Flask section + license | `ReCost` |
| `recost/__init__.py:1-5` docstring | `ReCost` |
| `recost/frameworks/flask.py:22` (class) | `ReCost` |
| `recost/frameworks/fastapi.py` (class) | `RecostMiddleware` |
| `CLAUDE.md:19, 27` | `EcoAPIHandle`, `EcoAPIConfig`, `EcoAPIMiddleware` — **none exist in code** |

The FastAPI middleware is `RecostMiddleware`, the Flask extension is `ReCost` — different conventions for analogous classes.

Stale doc claims piled on top:
- "21+ built-in rules" in both docs; actual count is **34 rules / 14 providers** (`_provider_registry.py:33` comment already says so).
- `README.md:106` documents `flush_interval: float (30.0)` as a top-level option; in code, `flush_interval` is the **deprecated** seconds option that emits a `DeprecationWarning`. The real option is `flush_interval_ms: int = 30_000`. Users who follow the README pull warnings into their logs.
- `flush_interval_ms`, `max_buckets`, `shutdown_flush_timeout_ms` are missing from the README config table.

**Fix:**
1. Pick one brand spelling (recommend `Recost`).
2. Rename `ReCost` (Flask) → `RecostExtension` (or `Recost`), with a deprecation alias for one release.
3. Strip every `EcoAPI*` reference from `CLAUDE.md`.
4. Walk the README: update provider count, swap to `flush_interval_ms` as the documented option (mark `flush_interval` deprecated), add the missing config fields.
5. Add a test that asserts `len(BUILTIN_PROVIDERS) == 34` so future drift is caught.

**Files:** `recost/frameworks/flask.py`, `recost/__init__.py`, `README.md`, `CLAUDE.md`, `tests/test_provider_registry.py`

**Includes:** original #4, #5, #6.

---

### 6. Process lifecycle: no `atexit` flush, no signal handlers, dispose can leak threads — P1

**Body:**

Two related lifecycle gaps:

1. **No `atexit` / signal handler.** The flush timer is a daemon thread — when the process exits normally (`sys.exit()`, end of `__main__`, AWS Lambda invocation completes, SIGTERM in a container), the daemon thread is killed and the current aggregator bucket is lost. Cron jobs, Lambdas, batch scripts, and one-shot CLIs report nothing unless the user manually calls `handle.dispose()`.

2. **Dispose during connect leaks FDs.** `recost/_transport.py:133-146, 199-210`'s `dispose()` sets `self._running = False` and queues a sentinel. If the loop is currently inside `websockets.connect(url)` (blocking await on TCP connect), the sentinel sits in the queue until the connect either succeeds or hits the OS TCP timeout (~75s on Linux). `thread.join(timeout=2.0)` returns without joining. The daemon thread + open socket FD leak until process exit. In long-lived processes that call `init()/dispose()` many times (test suites, Flask dev server with reload), FDs accumulate.

**Fix:**
- In `init()`, register `atexit.register(_final_flush)` and (optionally) `signal.signal(SIGTERM, ...)`. Make handlers idempotent. Respect `shutdown_flush_timeout_ms`. Provide `auto_shutdown_handlers=False` opt-out.
- On dispose, `loop.call_soon_threadsafe(loop.stop)` and cancel pending tasks. Then join with a timeout derived from `shutdown_flush_timeout_ms` (currently hardcoded 5s) and log if the thread didn't exit.

**Files:** `recost/_init.py`, `recost/_transport.py`, `tests/test_init.py`

**Includes:** original #9 (hardcoded join), #18 (no atexit), #19 (dispose leak).

---

### 7. Local-mode WebSocket: unbounded queue, infinite reconnect, no auth handshake — P1

**Body:**

Three related local-transport hardening issues:

1. **Queue unbounded.** `_transport.py:194` enqueues outbound messages with no cap. The re-queue path on disconnect (`_transport.py:176`) also uses `put_nowait`. A long extension outage = unbounded memory growth.

2. **Reconnect loops forever.** `_transport.py:148-184` retries `localhost:9847` forever (capped at 30s per attempt). A user who deploys with no `api_key` (default = local mode) and no VS Code extension running keeps a daemon thread retrying forever while events queue up. Typical misconfig; silent failure.

3. **No auth on the WS port.** The SDK sends serialized `WindowSummary` payloads to whatever process responds first on `127.0.0.1:9847`. Any local process can squat the port and silently sink all telemetry. Low risk on a dev machine, but easy to harden.

**Fix:**
- Cap the queue (e.g. `asyncio.Queue(maxsize=1000)`) with drop-oldest. Log a single warning on first drop; reset on reconnect.
- After N consecutive failed connects (e.g. 10), give up and emit `on_error` once. Detect "no `api_key` AND first connect failed" and warn loudly.
- Lightweight handshake: SDK opens, sends `{"type":"hello","sdk":"recost-py","version":...}`; extension replies `{"type":"ack"}`. On no-ack within N ms, drop the connection without sending payloads. Coordinated change in the VS Code extension repo.

**Files:** `recost/_transport.py`, `tests/test_transport.py`, plus a paired change in the extension.

**Includes:** original #8, #17, #24.

---

### 8. Interceptor body-size measurement is wrong for common patterns — P1

**Body:**

Three related body-sizing issues:

1. **aiohttp `json=` and `FormData` report 0 bytes.** `recost/_interceptor.py:317-325` checks only the `data=` kwarg. The most common aiohttp POST patterns (`session.post(url, json={...})`, `session.post(url, data=FormData(...))`, async-iterable bodies, `BytesIO`) all fall through and report 0.

2. **httpx streaming body silently materialized.** `_interceptor.py:198-201, 244-247` accesses `request.content` to compute body size. For ordinary requests built via client methods, `content` is bytes — fine. For users passing a custom streaming body (`httpx.Request("POST", url, content=async_iterator)`), accessing `content` reads and buffers the entire iterator. A large upload silently OOMs the process.

3. **Response body size derived from `Content-Length` only.** Chunked / streaming responses (LLM SSE streams) don't set the header; they always report `response_bytes=0`. The README's "response body size (bytes)" promise is partially false for streams.

**Fix:**
- For aiohttp: when `json=` is present, JSON-serialize and measure; for `FormData`, query its `_size`; for unknown body types, leave at 0 but document.
- For httpx: `isinstance(request.content, bytes)` check before reading. Non-bytes → skip size measurement.
- For responses: document the streaming caveat in README; optionally tee non-streaming response bodies.

**Files:** `recost/_interceptor.py`, `README.md`, `tests/test_interceptor.py`

**Includes:** original #15 (Content-Length), #21 (aiohttp json), #22 (httpx streaming).

---

### 9. Test gaps: aiohttp paths, privacy claim, self-instrumentation, 5xx retry — P1

**Body:**

Four explicit claims in the README have no test coverage:

- **aiohttp interceptor branch.** `_interceptor.py` patches `aiohttp.ClientSession._request`, but `tests/test_interceptor.py` only covers urllib3, httpx sync, and httpx async. Zero direct aiohttp tests.
- **No headers/bodies captured (privacy contract).** Add a test that asserts `RawEvent`-shaped output carries no dict-typed payload field, plus a test that issues a request with sensitive headers and confirms they don't surface.
- **No self-instrumentation.** Verify that `urllib.request` calls made from `_post_cloud` do not trigger the urllib3 patch.
- **5xx retry path.** `test_no_retry_on_4xx` exists but no positive test that `max_retries` actually runs `n` attempts with exponential backoff.

Also missing: deprecation-warning test for `flush_interval`, `would_overflow` early-flush path test, `handle.dispose()` actually stops new flushes, `_LocalTransport` graceful no-op when websockets missing, Flask graceful degradation when flask is missing.

**Fix:** add tests for each. The aiohttp tests should mirror the existing httpx async tests (success, 4xx capture, latency, response-bytes, double-count guard via reentrancy).

**Files:** `tests/test_interceptor.py`, `tests/test_transport.py`, `tests/test_init.py`, `tests/test_flask.py`

**Includes:** original #7, #14.

---

### 10. Flush loop hygiene: errors swallowed indefinitely without backoff — P2

**Body:**

`recost/_init.py:194-195` catches every exception in the flush loop and continues firing every `flush_interval_ms`. A deterministic bug (malformed metric, transport never reachable) silently re-fires forever, logging every cycle.

**Fix:** track consecutive failures; after N (e.g. 5), back off exponentially up to a ceiling, and surface via `on_error`.

**Files:** `recost/_init.py`, `tests/test_init.py`

**Includes:** original #10.

---

### 11. urllib3 wrapper maintenance: dead kwargs + brittle import-time defaults — P2

**Body:**

Two related urllib3-patch maintenance issues:

1. `_interceptor.py:108` lists `pool_connections` and `pool_maxsize` as kwargs on the `urlopen` wrapper. Neither is an actual `HTTPConnectionPool.urlopen` parameter — they're `PoolManager.__init__` parameters. The wrapper accepts and silently drops them.

2. `_interceptor.py:108` references `urllib3.util.Timeout.DEFAULT_TIMEOUT` as a **default-argument value at function-definition time**. If the installed urllib3 (or a future release) doesn't expose that attribute, the SDK fails to import — and the failure happens before any user code runs.

**Fix:**
- Remove the dead `pool_connections` / `pool_maxsize` params; rely entirely on `**kwargs` forwarding.
- Use a sentinel (`_DEFAULT = object()`) for the timeout default; resolve to the urllib3 default inside the body. Wrap import-time lookups in a try/except so a broken urllib3 install degrades to a no-op rather than crashing the host app.

**Files:** `recost/_interceptor.py`, `tests/test_interceptor.py`

**Includes:** original #11, #23.

---

### 12. `exclude_patterns` is unscoped substring; localhost not auto-excluded in cloud mode — P2

**Body:**

`_init.py:148-150` uses `pattern in event.url or pattern in event.host`. Short or hostname-like patterns over-match. `*` is taken literally, not as a glob — users naturally pass `"*.internal.corp"` expecting it to work and silently miss every request.

Also: when `api_key` is set (cloud mode), the SDK does not auto-exclude `localhost` / `127.0.0.1`. A local dev recost instance could be self-traced.

**Fix:**
- Add an option for exact host match (e.g. accept `("=", "api.example.com")` tuples or a separate `exclude_hosts` field).
- Document the substring contract explicitly; reject patterns containing `*` with a clear error so users don't misuse it.
- Auto-exclude localhost when a local recost dev API is detected.

**Files:** `recost/_init.py`, `README.md`, `tests/test_init.py`

**Includes:** original #12.

---

### 13. Code hygiene: unused import, traceback context dropped — P2

**Body:**

Two trivial fixes:

1. `ruff check` reports `F401 unused-import` for `MAX_BUCKETS` in `recost/_transport.py:23`.
2. `recost/_interceptor.py:148-156, 219-228, 266-275, 342-350` re-raise via `raise exc` rather than bare `raise`, rebinding the traceback. End users debugging SDK-wrapped errors see the SDK wrapper at the top of the stack, not their own call site.

**Fix:**
- Remove the unused import.
- Replace every `raise exc` with bare `raise`.

**Files:** `recost/_transport.py`, `recost/_interceptor.py`

**Includes:** original #13, #25.

---

## Filing checklist

- [ ] Open issues 1–13 in the Python repo.
- [ ] Label by priority (`P0` / `P1` / `P2`) and kind (`bug` / `docs` / `test` / `runtime`).
- [ ] Group milestones:
  - **Next patch release**: issues 1, 2, 3 — the three P0s. Issue 3 (fork-safety) is the single largest production gap; most Python deployments use prefork servers.
  - **Next minor release**: issues 4, 5, 6, 7, 8, 9 — P1 cluster (state safety, docs, lifecycle, transport, body sizing, test gaps).
  - **Backlog**: 10–13.
- [ ] Link 4, 6 — both touch the lifecycle / threading model; consider one consolidated PR.
- [ ] Link 5 (naming + docs) — pure documentation PR, easy to land first and unblock everything else.
- [ ] Link 7 — coordinated change with the VS Code extension repo for the handshake.

---

## Mapping back to original findings

This consolidation collapses 25 raw findings into 13 issues by grouping by fix location and shared rationale. Mapping:

| Consolidated | Original |
|---|---|
| 1 | #1 (aggregator race) |
| 2 | #2 (mypy errors) |
| 3 | #16 (fork-safety) |
| 4 | #3 (`_handle` race), #20 (init/dispose race) |
| 5 | #4 (naming), #5 (`flush_interval` deprecation), #6 (provider count) |
| 6 | #9 (hardcoded timer join), #18 (no atexit), #19 (dispose-during-connect leak) |
| 7 | #8 (queue unbounded), #17 (infinite reconnect), #24 (no auth) |
| 8 | #15 (Content-Length), #21 (aiohttp `json=`), #22 (httpx streaming) |
| 9 | #7 (aiohttp tests), #14 (privacy/self/5xx) |
| 10 | #10 (flush-loop errors) |
| 11 | #11 (dead kwargs), #23 (Timeout import-time) |
| 12 | #12 (exclude pattern) |
| 13 | #13 (ruff F401), #25 (`raise exc`) |
