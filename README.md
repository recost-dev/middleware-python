# ecoapi

Python SDK for [ReCost](https://recost.dev) — automatically tracks outbound HTTP API calls from your application and reports cost, latency, and usage patterns to the ReCost dashboard or your local VS Code extension.

## How it works

The SDK monkey-patches `urllib3`, `httpx`, and `aiohttp` to intercept outbound requests at runtime. It captures metadata only (URL, method, status, latency, byte sizes — never headers or bodies), matches each request against a built-in provider registry, aggregates events into time-windowed summaries, and ships those summaries either to the ReCost cloud API or to the ReCost VS Code extension running locally.

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
pip install ecoapi
```

With optional framework and local mode extras:

```bash
pip install ecoapi[fastapi]   # FastAPI/Starlette middleware
pip install ecoapi[flask]     # Flask extension
pip install ecoapi[local]     # WebSocket for VS Code extension
pip install ecoapi[all]       # Everything
```

## Quick start

### Local mode (VS Code extension)

No API key needed. Telemetry goes to the ReCost VS Code extension over localhost.

```python
from ecoapi import init

init()  # all defaults — local mode on port 9847
```

### Cloud mode

```python
import os
from ecoapi import init

init(EcoAPIConfig(
    api_key=os.environ["ECOAPI_KEY"],
    project_id=os.environ["ECOAPI_PROJECT_ID"],
    environment=os.environ.get("PYTHON_ENV", "development"),
))
```

### FastAPI

```python
from fastapi import FastAPI
from ecoapi.frameworks.fastapi import EcoAPIMiddleware

app = FastAPI()
app.add_middleware(EcoAPIMiddleware, api_key="...", project_id="...")
```

### Flask

```python
from flask import Flask
from ecoapi.frameworks.flask import EcoAPI

app = Flask(__name__)
eco = EcoAPI(app, api_key="...", project_id="...")
```

## Configuration

All fields are optional.

| Option | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | — | ReCost API key. If omitted, runs in local mode. |
| `project_id` | `str` | — | ReCost project ID. Required in cloud mode. |
| `environment` | `str` | `"development"` | Environment tag attached to all telemetry. |
| `flush_interval` | `float` | `30.0` | Seconds between automatic flushes. |
| `max_batch_size` | `int` | `100` | Early-flush threshold (number of events). |
| `local_port` | `int` | `9847` | WebSocket port for the VS Code extension. |
| `debug` | `bool` | `False` | Log telemetry activity to stderr. |
| `enabled` | `bool` | `True` | Master kill switch. Set `False` to disable in tests. |
| `custom_providers` | `list[ProviderDef]` | `[]` | Extra provider rules merged with higher priority than built-ins. |
| `exclude_patterns` | `list[str]` | `[]` | URL substrings that cause a request to be silently dropped. |
| `base_url` | `str` | `"https://api.recost.dev"` | Override for self-hosted deployments. |
| `max_retries` | `int` | `3` | Retry attempts for failed cloud flushes. |
| `on_error` | `Callable` | — | Called on internal SDK errors. |

### Custom providers

```python
from ecoapi import init, EcoAPIConfig, ProviderDef

init(EcoAPIConfig(
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
handle = init(EcoAPIConfig(api_key="..."))

# Later — e.g. in a test teardown or process shutdown handler:
handle.dispose()
```

### Disabling in tests

```python
import os
from ecoapi import init, EcoAPIConfig

init(EcoAPIConfig(enabled=os.environ.get("PYTHON_ENV") != "test"))
```

## Supported providers

The registry ships with built-in rules for these providers. Cost estimates are rough per-request averages for relative comparison — actual costs vary by model, token count, and region.

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

Unrecognized hosts produce a `RawEvent` with `provider=None` — they still appear in telemetry grouped under `"unknown"`.

## What is captured (and what is not)

**Captured:**
- Request timestamp, method, URL (query params stripped), host, path
- Response status code
- Round-trip latency (ms)
- Request and response body size (bytes)
- Matched provider, endpoint category, and estimated cost

**Never captured:**
- Request or response headers (contain API keys)
- Request or response body content (may contain user data or PII)

## Core types

```python
from ecoapi import (
    RawEvent,
    MetricEntry,
    WindowSummary,
    EcoAPIConfig,
    ProviderDef,
    TransportMode,
)
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## API reference

All requests go to `https://api.recost.dev`. No authentication is required for project management endpoints.

### Create a project

```bash
curl -s -X POST https://api.recost.dev/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app", "description": "optional"}' | jq .
```

Copy the `id` from the response — that's your `project_id`.

### List your projects

```bash
curl -s https://api.recost.dev/projects | jq .
```

### View analytics for a project

```bash
curl -s "https://api.recost.dev/projects/{project_id}/analytics" | jq .
```

### View cost breakdown by provider

```bash
curl -s "https://api.recost.dev/projects/{project_id}/cost/by-provider" | jq .
```

### Send telemetry manually (what the SDK does on flush)

```bash
curl -s -X POST https://api.recost.dev/projects/{project_id}/telemetry \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {api_key}" \
  -d @payload.json | jq .
```

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE) © 2026 Andres Lopez, Aslan Wang, Donggyu Yoon.
