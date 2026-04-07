# recost — Python Middleware

Python SDK that automatically tracks outbound HTTP API calls, matches them against a built-in provider registry, aggregates events into time-windowed summaries, and ships telemetry to the ReCost cloud API or VS Code extension.

## Tech Stack

- **Python ≥ 3.9** — no core dependencies
- **hatchling** — build backend
- **pytest** + **pytest-asyncio** — testing (130+ tests across 10 files)
- **ruff** — linting
- **mypy** — strict type checking
- Optional: `starlette`, `flask`, `websockets`

## Project Structure

```
recost/
  __init__.py               # Public API surface (re-exports only)
  _init.py                  # Main entry point — wires interceptor, registry, aggregator, transport; returns EcoAPIHandle
  _types.py                 # All types: RawEvent, MetricEntry, WindowSummary, ProviderDef, EcoAPIConfig, TransportMode
  _provider_registry.py     # ProviderRegistry — 21+ built-in rules, wildcard host matching, custom provider priority
  _interceptor.py           # Patches urllib3.HTTPConnectionPool.urlopen, httpx.Client.send, httpx.AsyncClient.send, aiohttp.ClientSession._request
  _aggregator.py            # Time-windowed bucketing by provider+endpoint+method, p50/p95 percentiles, cost aggregation
  _transport.py             # Cloud mode (HTTPS POST with retry) + local mode (WebSocket with reconnect on background thread)
  frameworks/
    __init__.py
    fastapi.py              # EcoAPIMiddleware — ASGI middleware for FastAPI/Starlette
    flask.py                # EcoAPI — Flask extension with init_app() pattern
tests/
  conftest.py               # Fixtures — cleanup interceptor after each test
  test_scaffold.py          # Smoke tests for public API exports
  test_types.py             # MetricEntry & WindowSummary serialization (camelCase conversion)
  test_provider_registry.py # All 21 built-in providers, wildcards, Twilio refinement, custom priority
  test_aggregator.py        # Flush, grouping, percentiles, error counting, byte sums, cost, null handling
  test_interceptor.py       # urllib3/requests, httpx sync+async, aiohttp, lifecycle, double-count prevention
  test_transport.py         # Mode detection, HTTP server mocking, retry logic
  test_init.py              # init/dispose lifecycle, disabled mode, double-init, exclude patterns
  test_flask.py             # Flask extension init, init_app, kwargs
  test_fastapi.py           # FastAPI middleware init, kwargs
pyproject.toml
LICENSE
```

## Commands

```bash
pip install -e ".[dev]"    # Install with dev dependencies
pytest                     # Run all tests
ruff check recost/         # Lint
mypy recost/               # Type check (strict mode)
```

## Architecture Notes

- **Zero core dependencies** — urllib.request used for cloud transport to avoid self-instrumentation
- **Three HTTP library patches**: urllib3 (used by `requests`), httpx (sync + async), aiohttp (async)
- **Reentrancy guard** via `contextvars` prevents double-counting when libraries call each other internally
- **Module-level singleton** in `_init.py` prevents multiple simultaneous initializations
- **Background thread** for flush timer and local WebSocket transport (uses daemon threads + `threading.Timer`)
- **Graceful degradation** — missing optional dependencies (starlette, flask, websockets) are handled with ImportError
- **Privacy first** — query params stripped from URLs, headers and body content never captured
- Framework adapters are thin wrappers that call `init()` internally

## Provider Registry

21+ built-in rules covering:
- **AI**: OpenAI (6 endpoint rules), Anthropic (2 rules)
- **Payments**: Stripe (5 rules)
- **Communication**: Twilio (1 rule with dynamic SMS/voice refinement), SendGrid (2 rules)
- **Infrastructure**: Pinecone (3 rules), AWS (wildcard), Google Cloud (wildcard)
- **Other**: GitHub (4 rules), CoinGecko (3 rules), Hacker News (3 rules), wttr.in, ZenQuotes, ip-api

Custom providers are prepended before built-ins (higher priority). Unrecognized hosts grouped under `"unknown"`.

## Transport Modes

- **Cloud mode** (when `api_key` is provided): HTTPS POST to `api.recost.dev` with exponential-backoff retry (max 3 attempts, 4xx skips retry)
- **Local mode** (default): WebSocket to `localhost:9847` (VS Code extension), background thread with async event loop, auto-reconnect on connection loss
