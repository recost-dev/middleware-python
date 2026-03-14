"""
Core type definitions for the ecoapi Python SDK.
Every other module imports from here. No runtime logic, no external imports.

These produce the exact same JSON field names as the Node SDK's types.ts when
serialized via to_dict(). The API ingestion endpoint doesn't care which language
generated the telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Literal, Optional


# ---------------------------------------------------------------------------
# RawEvent
# ---------------------------------------------------------------------------

@dataclass
class RawEvent:
    """A single intercepted outbound HTTP request."""

    timestamp: str
    method: str
    url: str
    host: str
    path: str
    status_code: int
    latency_ms: int
    request_bytes: int
    response_bytes: int
    provider: Optional[str] = None
    endpoint_category: Optional[str] = None
    error: bool = False


# ---------------------------------------------------------------------------
# MetricEntry
# ---------------------------------------------------------------------------

@dataclass
class MetricEntry:
    """Aggregated stats for one provider + endpoint + method group."""

    provider: str
    endpoint: str
    method: str
    request_count: int
    error_count: int
    total_latency_ms: int
    p50_latency_ms: int
    p95_latency_ms: int
    total_request_bytes: int
    total_response_bytes: int
    estimated_cost_cents: float

    def to_dict(self) -> dict:
        """Serialize to camelCase dict matching the Node SDK / API contract."""
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "method": self.method,
            "requestCount": self.request_count,
            "errorCount": self.error_count,
            "totalLatencyMs": self.total_latency_ms,
            "p50LatencyMs": self.p50_latency_ms,
            "p95LatencyMs": self.p95_latency_ms,
            "totalRequestBytes": self.total_request_bytes,
            "totalResponseBytes": self.total_response_bytes,
            "estimatedCostCents": self.estimated_cost_cents,
        }


# ---------------------------------------------------------------------------
# WindowSummary
# ---------------------------------------------------------------------------

@dataclass
class WindowSummary:
    """What the aggregator produces on flush. Sent to cloud API or local extension."""

    project_id: str
    environment: str
    sdk_language: str
    sdk_version: str
    window_start: str
    window_end: str
    metrics: List[MetricEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to camelCase dict matching the API contract."""
        return {
            "projectId": self.project_id,
            "environment": self.environment,
            "sdkLanguage": self.sdk_language,
            "sdkVersion": self.sdk_version,
            "windowStart": self.window_start,
            "windowEnd": self.window_end,
            "metrics": [m.to_dict() for m in self.metrics],
        }


# ---------------------------------------------------------------------------
# ProviderDef
# ---------------------------------------------------------------------------

@dataclass
class ProviderDef:
    """A single provider matching rule for the provider registry."""

    host_pattern: str
    provider: str
    path_prefix: Optional[str] = None
    endpoint_category: Optional[str] = None
    cost_per_request_cents: Optional[float] = None


# ---------------------------------------------------------------------------
# EcoAPIConfig
# ---------------------------------------------------------------------------

@dataclass
class EcoAPIConfig:
    """Configuration passed to init() or a framework wrapper. All fields optional."""

    api_key: Optional[str] = None
    project_id: Optional[str] = None
    environment: str = "development"
    flush_interval: float = 30.0
    max_batch_size: int = 100
    local_port: int = 9847
    debug: bool = False
    enabled: bool = True
    custom_providers: List[ProviderDef] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)
    base_url: str = "https://api.ecoapi.dev"
    max_retries: int = 3
    on_error: Optional[Callable[[Exception], None]] = None


# ---------------------------------------------------------------------------
# TransportMode
# ---------------------------------------------------------------------------

TransportMode = Literal["local", "cloud"]
