# recost — Python Middleware

Python SDK that automatically tracks outbound HTTP API calls, matches them against a built-in provider registry, aggregates events into time-windowed summaries, and ships telemetry to the Recost cloud API, the VS Code extension, or an NDJSON file on disk.

## Tech Stack

- **Python ≥ 3.9** — no core dependencies
- **hatchling** — build backend
- **pytest** + **pytest-asyncio** — testing (224 tests across 14 files)
- **ruff** — linting
- **mypy** — strict type checking
- Optional: `starlette`, `flask`, `websockets`

## Project Structure

```
recost/
  __init__.py               # Public API surface (re-exports only)
  _init.py                  # init/RecostHandle — wires interceptor, registry, aggregator, transport;
                            # owns lifecycle: atexit hook, os.register_at_fork hook, PID backstop, RLock'd init/dispose
  _types.py                 # All types: RawEvent, MetricEntry, WindowSummary, ProviderDef, RecostConfig,
                            # FlushStatus, TransportMode, LocalTransportMode, iso_now_ms_z(),
                            # error hierarchy (RecostError, RecostAuthError, RecostFatalAuthError, RecostRateLimitError)
  _provider_registry.py     # ProviderRegistry — 34 built-in rules (14 providers), wildcard host matching,
                            # custom provider priority, Twilio SMS/voice refinement
  _interceptor.py           # Patches urllib3.HTTPConnectionPool.urlopen, httpx.Client.send,
                            # httpx.AsyncClient.send, aiohttp.ClientSession._request;
                            # dual reentrancy guard (contextvars + threading.local),
                            # RLock'd install/uninstall, urlparse-based query/fragment/userinfo stripping
  _aggregator.py            # Time-windowed bucketing by provider+endpoint+method, p50/p95 percentiles,
                            # cost aggregation, MAX_BUCKETS=2000 cap with would_overflow() pre-flush, RLock'd ingest
  _transport.py             # Cloud (HTTPS POST + exp-backoff retry, 401 escalation, 429 Retry-After deferral)
                            # _LocalFileTransport (NDJSON append) + _LocalTransport (WebSocket, opt-in)
  frameworks/
    __init__.py
    fastapi.py              # RecostMiddleware — ASGI middleware for FastAPI/Starlette
    flask.py                # RecostExtension — Flask extension with init_app() pattern
                            # (ReCost is a deprecated alias kept one release with DeprecationWarning)
tests/
  conftest.py               # Fixtures — cleanup interceptor after each test
  test_scaffold.py          # Smoke tests for public API exports
  test_types.py             # MetricEntry & WindowSummary serialization (camelCase conversion, protocolVersion)
  test_provider_registry.py # All 34 built-in provider rules, wildcards, Twilio refinement, custom priority,
                            # BUILTIN_PROVIDERS count pinned at 34
  test_aggregator.py        # Flush, grouping, percentiles, error counting, byte sums, cost, null handling,
                            # MAX_BUCKETS overflow pre-flush, concurrent-ingest race
  test_interceptor.py       # urllib3/requests, httpx sync+async, aiohttp, lifecycle, double-count prevention,
                            # streaming bodies never materialized, fragment/userinfo stripping
  test_transport.py         # Mode detection, HTTP server mocking, 5xx retry w/ exp backoff,
                            # 401 escalation + fatal-suspend at N, 429 Retry-After deferral,
                            # graceful no-op when websockets missing
  test_init.py              # init/dispose lifecycle, disabled mode, double-init, exclude patterns,
                            # exclude_hosts exact match, atexit-at-process-exit, flush-loop backoff,
                            # api_key 'rc-' prefix validation, deprecation warnings
  test_flask.py             # Flask extension init, init_app idempotency, kwargs, missing-dep ImportError
  test_fastapi.py           # FastAPI middleware init, kwargs
  test_errors.py            # Typed error classes (RecostAuthError, RecostFatalAuthError, RecostRateLimitError)
  test_contract.py          # Wire-format contract: camelCase keys, protocolVersion="1.0", no projectId on wire
  test_fork_safety.py       # register_at_fork hook rebuilds timer/transport in child, PID backstop path
  test_reentrancy.py        # contextvars + threading.local dual guard prevents double-count across task/thread hops
pyproject.toml
LICENSE
ROADMAP.md                  # Open issues / planned work tracker
AUDIT.md                    # Audit notes from prior production-readiness reviews
```

## Commands

```bash
pip install -e ".[dev]"    # Install with dev dependencies
pytest                     # Run all 224 tests
ruff check recost/         # Lint
mypy recost/               # Type check (strict mode)
```

## Architecture Notes

- **Zero core dependencies** — `urllib.request` used for cloud transport to avoid self-instrumentation
- **Three HTTP library patches**: urllib3 (used by `requests`), httpx (sync + async), aiohttp (async)
- **Dual reentrancy guard** — `contextvars` (async-task scope) + `threading.local` (OS-thread scope) prevents double-counting when libraries call each other internally across either hop
- **Module-level singleton** in `_init.py` (`_handle`) plus an `RLock` on init/dispose prevents races and inconsistent state under concurrent callers
- **Process lifecycle** — `atexit` hook (gated by `auto_shutdown_handlers`, default True) runs a final flush at normal termination; idempotent with explicit `dispose()`
- **Fork safety** — `os.register_at_fork(after_in_child=...)` rebuilds transport + flush timer in the child; on platforms or wrappers (uwsgi lazy-fork) where the hook does not fire, a PID-backstop in the event path self-repairs on the first intercepted call
- **Background thread** for the flush timer and the local WebSocket transport (daemon threads + `threading.Event` stopping)
- **Graceful degradation** — missing optional deps (starlette, flask, websockets) are handled with ImportError stubs; the SDK never imports them at top level
- **Privacy first** — query strings, fragments, and userinfo are stripped from URLs via `urlparse`; headers and body content are never captured (only sizes)
- **Streaming-safe** — httpx streaming bodies and aiohttp async iterables are never materialized; size is reported as 0 instead of buffering
- Framework adapters are thin wrappers that call `init()` internally

## Provider Registry

34 built-in rules across 14 providers:
- **AI**: OpenAI (6 endpoint rules), Anthropic (2 rules)
- **Payments**: Stripe (5 rules)
- **Communication**: Twilio (1 rule with dynamic SMS/voice refinement), SendGrid (2 rules)
- **Infrastructure**: Pinecone (3 rules), AWS (wildcard), Google Cloud (wildcard)
- **Other**: GitHub (4 rules), CoinGecko (3 rules), Hacker News (3 rules), wttr.in, ZenQuotes, ip-api

Custom providers are prepended before built-ins (higher priority). Unrecognized hosts grouped under `"unknown"`. `BUILTIN_PROVIDERS` count is pinned by a test so accidental registry additions can't drift this number silently.

## Transport Modes

- **Cloud mode** (when `api_key` is set, must begin with `rc-` — validated at `init()`): HTTPS POST to `api.recost.dev/projects/{project_id}/telemetry` via `urllib.request`, with:
  - Exponential-backoff retry on 5xx / network errors (max 3 attempts, capped at 10s)
  - 4xx skips retry; response bodies are never read (only `x-request-id`)
  - **401 escalation**: each 401 emits `RecostAuthError` to `on_error`; after `max_consecutive_auth_failures` consecutive 401s (default 5) the transport suspends and emits a terminal `RecostFatalAuthError` + a second stderr line. Any non-401 outcome resets the streak.
  - **429 deferral**: parses `Retry-After` (seconds or HTTP-date), emits `RecostRateLimitError`, and defers the next flush via a shared deferral cell rather than dropping the window.
  - Per-window bucket cap: payloads with > `max_buckets` metrics are chunked and sent sequentially.

- **Local mode** (no `api_key`): one of two transports, selected by `local_transport`:
  - `"file"` (default) — `_LocalFileTransport`: NDJSON append-only to `$RECOST_LOCAL_DIR/{project_id}.jsonl` or `~/.recost/local-telemetry/{project_id}.jsonl`. POSIX chmod 0o600. Append-atomic for writes ≤ PIPE_BUF.
  - `"ws"` (opt-in) — `_LocalTransport`: WebSocket to `localhost:{local_port}` (default 9847). Bounded queue (1000 frames, drop-oldest) + capped reconnect attempts (10) with jittered exponential backoff. The VS Code extension does **not** currently host a WS server — keep it on `"file"` unless you've stood up your own listener.

Every wire frame carries a top-level `protocolVersion: "1.0"`. Consumers must reject frames with an unknown MAJOR version.

## Error hierarchy

Re-exported from the package root for `on_error` callbacks:

- `RecostError` — base class for all typed SDK errors
- `RecostAuthError(status, consecutive_failures)` — 401 received; transport still active
- `RecostFatalAuthError(...)` — subclass of `RecostAuthError`; transport suspended after N consecutive 401s
- `RecostRateLimitError(retry_after_ms, endpoint)` — 429 received; next flush deferred

## Handle API

`init()` returns `RecostHandle` with:
- `dispose()` — stop intercepting, run final flush bounded by `shutdown_flush_timeout_ms`, close transport. Idempotent.
- `flush_blocking(timeout_s=3.0) -> bool` — synchronously run a final flush bounded by `timeout_s`. Returns True on completion, False on timeout. Does NOT dispose. Parity with Node's awaited dispose.
- `reinit_after_fork()` — recreate transport + timer in current PID. Wired automatically via `os.register_at_fork`; also exposed for environments where the hook doesn't fire.
- `last_flush_status` — `Optional[FlushStatus]` with `status` ("ok"/"error"), `window_size`, `timestamp` (ms epoch).
- `pid` — PID this handle was last initialized in. Used by the PID-backstop check in the event path.
