# recost

Python SDK for [Recost](https://recost.dev) — automatically tracks outbound HTTP API calls from your application and reports cost, latency, and usage patterns to the Recost dashboard or your local VS Code extension.

**Requires Python 3.9+. No core dependencies.**

## How it works

The SDK patches `urllib3`, `httpx`, and `aiohttp` to intercept outbound requests at runtime. It captures metadata only (URL, method, status, latency, byte sizes — never headers or bodies), matches each request against a built-in provider registry, aggregates events into time-windowed summaries, and ships those summaries to the Recost cloud API or the Recost VS Code extension running locally.

```
Your app
  └─ requests.get("https://api.openai.com/v1/chat/completions", ...)
       │
       ▼
  Interceptor               ← patches urllib3, httpx, aiohttp
       │  RawEvent { host, path, method, status_code, latency_ms, ... }
       ▼
  ProviderRegistry          ← matches host/path → provider + endpoint_category + cost
       │
       ▼
  Aggregator                ← buffers events, flushes WindowSummary every 30s
       │
       ▼
  Transport
    ├─ local mode  → WebSocket  → VS Code extension (port 9847)
    └─ cloud mode  → HTTPS POST → api.recost.dev
```

## Installation

```bash
pip install recost
```

With optional framework and local mode extras:

```bash
pip install recost[fastapi]   # FastAPI/Starlette middleware
pip install recost[flask]     # Flask extension
pip install recost[local]     # WebSocket transport for VS Code extension
pip install recost[all]       # Everything
```

## Quick start

### Local mode (VS Code extension)

No API key needed. Telemetry goes to the Recost VS Code extension over localhost.

```python
from recost import init

init()  # defaults — local mode on port 9847
```

### Cloud mode

```python
import os
from recost import init, RecostConfig

init(RecostConfig(
    api_key=os.environ["RECOST_API_KEY"],
    project_id=os.environ["RECOST_PROJECT_ID"],
    environment=os.environ.get("PYTHON_ENV", "development"),
))
```

### FastAPI

```python
from fastapi import FastAPI
from recost.frameworks.fastapi import RecostMiddleware

app = FastAPI()
app.add_middleware(RecostMiddleware, api_key="...", project_id="...")
```

### Flask

```python
from flask import Flask
from recost.frameworks.flask import RecostExtension

app = Flask(__name__)
RecostExtension(app, api_key="...", project_id="...")
```

Or using the `init_app` pattern:

```python
ext = RecostExtension()
ext.init_app(app, api_key="...", project_id="...")
```

> **Note:** the old class name `ReCost` is still importable as a deprecated
> alias and will continue to work for one release with a `DeprecationWarning`.
> Migrate to `RecostExtension`.

## Configuration

All fields are optional. Pass them as keyword arguments or via a `RecostConfig` instance.

| Option | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | — | Recost API key (`rc-...`). If omitted, runs in local mode. |
| `project_id` | `str` | — | Recost project ID. Required in cloud mode. |
| `environment` | `str` | `"development"` | Environment tag attached to all telemetry. |
| `flush_interval_ms` | `int` | `30000` | Milliseconds between automatic aggregator flushes. |
| `flush_interval` | `float` | — | **Deprecated.** Legacy seconds-based flush interval. If set, takes precedence over `flush_interval_ms` and emits a `DeprecationWarning`. Will be removed in a future release. |
| `max_batch_size` | `int` | `100` | Early-flush threshold (number of events). |
| `max_buckets` | `int` | `2000` | Maximum unique (provider, endpoint, method) triplets per window. Crossing this triggers an early flush. |
| `local_port` | `int` | `9847` | WebSocket port for the VS Code extension. |
| `local_transport` | `Literal["file", "ws"]` | `"file"` | Which local-mode transport to use. `"file"` (default) writes NDJSON to `~/.recost/local-telemetry/{project_id}.jsonl`. `"ws"` opts into a WebSocket to `localhost:{local_port}` (no server hosts this by default — see [extension#91](https://github.com/recost-dev/extension/issues/91)). |
| `debug` | `bool` | `False` | Log telemetry activity to stderr. |
| `enabled` | `bool` | `True` | Master kill switch — set `False` to disable entirely. |
| `custom_providers` | `list[ProviderDef]` | `[]` | Extra provider rules with higher priority than built-ins. |
| `exclude_patterns` | `list[str]` | `[]` | URL substrings — matching requests are silently dropped. |
| `exclude_hosts` | `list[str]` | `[]` | Exact host names to exclude (event.host match). Use for unambiguous host-level exclusion without substring false-positives. |
| `base_url` | `str` | `"https://api.recost.dev"` | Override for self-hosted deployments. |
| `max_retries` | `int` | `3` | Retry attempts for failed cloud flushes. |
| `shutdown_flush_timeout_ms` | `int` | `3000` | How long `dispose()` waits for the final flush to complete before closing the transport. |
| `max_consecutive_auth_failures` | `int` | `5` | Cloud transport suspends after this many consecutive 401 responses. Reset on any non-401 outcome. Matches Node's `maxConsecutiveAuthFailures`. |
| `auto_shutdown_handlers` | `bool` | `True` | When `True`, `init()` registers an `atexit` hook that runs the final flush at normal process termination. Set `False` if the host application manages its own lifecycle and does not want recost touching `atexit`. |
| `on_error` | `Callable[[Exception], None]` | — | Called on internal SDK errors. See [Error handling](#error-handling) for the typed exception classes you can dispatch on. |

> **Note on `api_key`:** must be a string beginning with `rc-`. `init()` raises `ValueError` at startup otherwise — telemetry is never silently sent with a malformed key.

> **Note on exclusions:** `exclude_patterns` performs substring matching against both `event.url` and `event.host`; patterns containing `*` raise `ValueError` at init time (substring matching is not glob). For unambiguous host-level exclusion without substring false-positives (e.g., excluding `api.example.com` without also dropping `myapi.example.com`), use `exclude_hosts` instead. Both are applied additively — events matching either are dropped before reaching the aggregator.

### Local-mode transports

When no `api_key` is set, the SDK runs in **local mode**. Two transports are available:

#### File (default — recommended)

`local_transport="file"`: each `WindowSummary` is appended as one NDJSON line to:

```
$RECOST_LOCAL_DIR/{project_id}.jsonl     # if RECOST_LOCAL_DIR is set
~/.recost/local-telemetry/{project_id}.jsonl  # otherwise (POSIX & macOS)
```

If `project_id` is empty, the file is named `default.jsonl`.

On POSIX systems the file is `chmod`'d to `0o600` (owner read/write only). On Windows, the ACL is not adjusted — Python's `chmod` is mostly a no-op there.

Multi-process writes from different processes targeting the same `project_id` are safe for typical telemetry frames (POSIX `O_APPEND` is atomic for writes ≤ `PIPE_BUF`, ~4 KB on Linux). Very large frames may interleave across processes.

If the directory can't be created or the file can't be opened (`PermissionError`, disk full), `on_error` fires once per failure-episode and subsequent writes are silently dropped until the next successful write.

#### WebSocket (opt-in)

`local_transport="ws"`: opens `ws://127.0.0.1:{local_port}` (default port 9847). The VS Code extension does not currently host a WS server, so this is only useful if you've stood up your own listener.

Hardening for opt-in WS users:
- Outbound queue is capped at 1000 frames with drop-oldest semantics. The first dropped frame fires `on_error` once per overflow episode (cleared on reconnect).
- After 10 consecutive failed reconnect attempts, the transport gives up and fires `on_error` once with a message pointing back to `local_transport="file"`.

#### Wire format

Every frame on every transport carries a top-level `protocolVersion: "1.0"` field. Consumers must reject frames with an unknown MAJOR version; MINOR bumps are forward-compatible.

### Custom providers

```python
from recost import init, RecostConfig, ProviderDef

init(RecostConfig(
    custom_providers=[
        ProviderDef(
            host_pattern="api.internal.acme.com",
            path_prefix="/payments",
            provider="acme-payments",
            endpoint_category="charge",
            cost_per_request_cents=0.5,
        ),
    ],
))
```

### Cleanup / teardown

`init()` returns a handle with a `dispose()` method that stops the interceptor, cancels the flush timer, and closes the transport connection.

```python
handle = init(RecostConfig(api_key="..."))

# In a test teardown or shutdown handler:
handle.dispose()
```

#### `handle.flush_blocking(timeout_s: float = 3.0) -> bool`

Synchronously runs the final flush on the calling thread, bounded by
`timeout_s` seconds. Returns `True` if the flush completed within the
budget, `False` on timeout.

Companion to `dispose()` for callers that need a hard ordering guarantee
the last window was sent — short-lived scripts, `os._exit()` paths,
test teardown. Unlike `dispose()`, this does NOT stop the periodic
timer or close the transport, and may be called multiple times. Brings
Python to parity with Node's `await handle.dispose()`, which awaits
the final flush by default.

```python
from recost import init, RecostConfig
import sys

handle = init(RecostConfig(api_key="..."))
# ... your code ...
if not handle.flush_blocking(timeout_s=3.0):
    print("warning: telemetry flush did not settle within 3s", file=sys.stderr)
handle.dispose()
```

### Disabling in tests

```python
import os
from recost import init, RecostConfig

init(RecostConfig(enabled=os.environ.get("PYTHON_ENV") != "test"))
```

## Error handling

`on_error` receives both arbitrary `Exception` instances and four typed errors you can dispatch on. All four inherit from `RecostError`, which itself inherits from `Exception`.

```python
from recost import (
    init, RecostConfig,
    RecostError, RecostAuthError, RecostFatalAuthError, RecostRateLimitError,
)

def on_error(exc: Exception) -> None:
    if isinstance(exc, RecostFatalAuthError):
        # Transport has suspended itself — telemetry stops until process restart.
        # Rotate the API key, ship a new build, then restart.
        page_on_call(exc)
    elif isinstance(exc, RecostAuthError):
        # 401 received but not yet at the fatal threshold.
        log.warning("recost: auth failure %d/%d", exc.consecutive_failures, 5)
    elif isinstance(exc, RecostRateLimitError):
        # 429 received — the SDK has already deferred the next flush.
        log.info("recost: rate-limited, deferred %dms", exc.retry_after_ms)
    elif isinstance(exc, RecostError):
        log.info("recost: %s", exc)

init(RecostConfig(api_key="...", on_error=on_error))
```

- `RecostAuthError(status, consecutive_failures)` — fired on every 401 response.
- `RecostFatalAuthError(...)` — subclass of `RecostAuthError`; fired once when the consecutive-401 streak reaches `max_consecutive_auth_failures`. After this, `transport.send()` becomes a silent no-op until the process restarts (the SDK assumes the key is permanently wrong, not transiently rejected).
- `RecostRateLimitError(retry_after_ms, endpoint)` — fired on a 429. The SDK has already parsed `Retry-After` and deferred the next flush — you do not need to take action; this is just a heads-up for logging.

### Fork safety

In environments that fork worker processes (Gunicorn, uWSGI, multiprocessing pools), the SDK automatically re-initializes the flush timer and transport in each child:

- On any platform that supports `os.register_at_fork`, the SDK installs an `after_in_child` hook that runs `handle.reinit_after_fork()` for you.
- For wrappers that bypass that hook (uWSGI lazy-fork, some embedded runtimes), the first intercepted outbound call in the child triggers the rebuild via a PID backstop check. The first time this fires, `on_error` is called once with a `RecostError` describing what happened.
- You can also call `handle.reinit_after_fork()` explicitly from your own post-fork hook. It is idempotent within a PID — a no-op if the timer thread is already alive in the current process.

### Process lifecycle

For short-lived processes (CLI scripts, cron jobs, Lambda functions, SIGTERM'd containers) the flush timer runs on a daemon thread and dies on exit. `init()` therefore registers an `atexit` handler by default that runs the final flush at normal termination. It delegates to the same idempotent `dispose()` you can call explicitly. Disable with `auto_shutdown_handlers=False` if your host application owns lifecycle.

For paths that bypass `atexit` (`os._exit`, signal-handler exits, test runners that hard-kill workers), call `handle.flush_blocking(timeout_s=...)` to guarantee the last window settles before you tear the process down.

### Observing flush outcomes

```python
handle = init(RecostConfig(api_key="rc-..."))
# ... after some traffic ...
status = handle.last_flush_status  # FlushStatus | None
if status is not None and status.status == "error":
    log.warning("recost: last flush errored, window_size=%d", status.window_size)
```

`last_flush_status` reflects only the most recent flush — it's a heartbeat for dashboards or health checks, not a complete event stream. For per-flush observation, use `on_error`.

## Supported providers

Built-in rules ship for the providers below. Cost estimates are rough per-request averages for relative comparison — actual costs vary by model, token count, and region.

| Provider | Host | Tracked endpoints | Cost estimate |
|---|---|---|---|
| **OpenAI** | `api.openai.com` | chat completions, embeddings, image generation, audio transcription, TTS | 0.01–4.0¢/req |
| **Anthropic** | `api.anthropic.com` | messages | 1.5¢/req |
| **Stripe** | `api.stripe.com` | charges, payment intents, customers, subscriptions | 0¢ (% billing) |
| **Twilio** | `api.twilio.com` | SMS, voice calls | 0.79–1.3¢/req |
| **SendGrid** | `api.sendgrid.com` | mail send | 0.1¢/req |
| **Pinecone** | `*.pinecone.io` | vector upsert, query | 0.08¢/req |
| **AWS** | `*.amazonaws.com` | all services (wildcard) | 0¢ (complex pricing) |
| **Google Cloud** | `*.googleapis.com` | all services (wildcard) | 0¢ (complex pricing) |

Unrecognized hosts still appear in telemetry, grouped under `"unknown"`.

## What is captured (and what is not)

**Captured:**
- Request timestamp, method, URL (query params stripped), host, path
- Response status code
- Round-trip latency (ms)
- Request body size (bytes) — measured for JSON, form, bytes, and string payloads. Streaming uploads (async iterators, generators) are reported as 0 to avoid buffering large bodies.
- Response body size (bytes) — derived from the `Content-Length` response header. HTTP chunked and SSE streams do not set this header and will report 0.
- Matched provider, endpoint category, and estimated cost

**Never captured:**
- Request or response headers (may contain API keys)
- Request or response body content (may contain user data or PII)

## Core types

```python
from recost import (
    # Lifecycle
    init, RecostHandle,
    # Data shapes
    RawEvent,            # A single intercepted HTTP request
    MetricEntry,         # Aggregated stats for one provider + endpoint + method
    WindowSummary,       # Flush payload sent to the API, VS Code extension, or local file
    FlushStatus,         # Outcome of the most recent flush
    # Configuration
    RecostConfig,
    ProviderDef,         # A custom provider matching rule
    TransportMode,       # Literal["local", "cloud"]
    LocalTransportMode,  # Literal["file", "ws"]
    # Errors (all inherit from RecostError, which inherits from Exception)
    RecostError,
    RecostAuthError,
    RecostFatalAuthError,
    RecostRateLimitError,
    # Lower-level building blocks (most users won't need these)
    ProviderRegistry, MatchResult, BUILTIN_PROVIDERS,
    install, uninstall, is_installed,
    Aggregator, MAX_BUCKETS,
)
```

## Development

```bash
pip install -e ".[dev]"
pytest          # run all tests
ruff check .    # lint
mypy recost/    # type check
```

## API reference

All requests go to `https://api.recost.dev`. Authentication uses a `rc-` prefixed API key as `Authorization: Bearer {api_key}`.

### Send telemetry (what the SDK does on flush)

```bash
curl -s -X POST https://api.recost.dev/projects/{project_id}/telemetry \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {api_key}" \
  -d @payload.json | jq .
```

### View recent telemetry windows

```bash
curl -s "https://api.recost.dev/projects/{project_id}/telemetry/recent?limit=10" \
  -H "Authorization: Bearer {api_key}" | jq .
```

### View analytics

```bash
curl -s "https://api.recost.dev/projects/{project_id}/analytics?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z" \
  -H "Authorization: Bearer {api_key}" | jq .
```

## License

Licensed under the [Business Source License 1.1](LICENSE) © 2026 Andres Lopez, Aslan Wang, Donggyu Yoon. Converts to Apache 2.0 on 2030-04-02.
