"""
Aggregator — collects RawEvents into time-windowed buckets and produces
a compressed WindowSummary on flush.

Pure data structure: no I/O, no timers, no side effects.
Direct port of the Node SDK's aggregator.ts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ._types import MetricEntry, RawEvent, WindowSummary


# ---------------------------------------------------------------------------
# Internal bucket structure
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    provider: str
    endpoint: str
    method: str
    request_count: int = 0
    error_count: int = 0
    latencies: List[int] = field(default_factory=list)
    total_request_bytes: int = 0
    total_response_bytes: int = 0
    estimated_cost_cents: float = 0.0


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------

def _compute_percentile(sorted_values: List[int], p: float) -> int:
    if len(sorted_values) == 0:
        return 0
    idx = math.ceil(len(sorted_values) * p) - 1
    idx = max(0, min(idx, len(sorted_values) - 1))
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# Aggregator class
# ---------------------------------------------------------------------------

class Aggregator:
    """
    Collects RawEvents into per-(provider, endpoint, method) buckets and
    compresses them into a WindowSummary on flush.
    """

    def __init__(
        self,
        project_id: str = "",
        environment: str = "development",
        sdk_version: str = "0.0.0",
    ) -> None:
        self._project_id = project_id
        self._environment = environment
        self._sdk_version = sdk_version
        self._buckets: Dict[str, _Bucket] = {}
        self._window_start: Optional[str] = None
        self._size = 0

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def ingest(self, event: RawEvent, cost_cents: float = 0.0) -> None:
        """Add one RawEvent to the current window."""
        if self._window_start is None:
            self._window_start = event.timestamp

        provider = event.provider if event.provider is not None else "unknown"
        endpoint = event.endpoint_category if event.endpoint_category is not None else event.path
        key = f"{provider}::{endpoint}::{event.method}"

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(provider=provider, endpoint=endpoint, method=event.method)
            self._buckets[key] = bucket

        bucket.request_count += 1
        if event.error:
            bucket.error_count += 1
        bucket.latencies.append(event.latency_ms)
        bucket.total_request_bytes += event.request_bytes
        bucket.total_response_bytes += event.response_bytes
        bucket.estimated_cost_cents += cost_cents

        self._size += 1

    def flush(self) -> Optional[WindowSummary]:
        """Compress the current window into a WindowSummary and reset state."""
        if not self._buckets:
            return None

        window_start = self._window_start or datetime.now(timezone.utc).isoformat()
        window_end = datetime.now(timezone.utc).isoformat()

        metrics: List[MetricEntry] = []

        for bucket in self._buckets.values():
            sorted_latencies = sorted(bucket.latencies)
            total_latency_ms = sum(sorted_latencies)

            metrics.append(MetricEntry(
                provider=bucket.provider,
                endpoint=bucket.endpoint,
                method=bucket.method,
                request_count=bucket.request_count,
                error_count=bucket.error_count,
                total_latency_ms=total_latency_ms,
                p50_latency_ms=_compute_percentile(sorted_latencies, 0.5),
                p95_latency_ms=_compute_percentile(sorted_latencies, 0.95),
                total_request_bytes=bucket.total_request_bytes,
                total_response_bytes=bucket.total_response_bytes,
                estimated_cost_cents=bucket.estimated_cost_cents,
            ))

        # Reset
        self._buckets = {}
        self._window_start = None
        self._size = 0

        return WindowSummary(
            project_id=self._project_id,
            environment=self._environment,
            sdk_language="python",
            sdk_version=self._sdk_version,
            window_start=window_start,
            window_end=window_end,
            metrics=metrics,
        )

    @property
    def size(self) -> int:
        """Total events ingested since the last flush."""
        return self._size

    @property
    def bucket_count(self) -> int:
        """Number of unique provider + endpoint + method groups."""
        return len(self._buckets)
