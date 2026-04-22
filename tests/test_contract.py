"""
Cross-SDK payload contract test.

Build a WindowSummary the same way _init.py does (Aggregator.flush()),
serialize it via WindowSummary.to_dict() (matching what Transport sends on
the wire), then assert the exact set of top-level fields and per-MetricEntry
fields the cloud API expects. The matching test in middleware-node
(tests/contract.test.ts) asserts the identical schema.

If anyone renames or re-units a field on either side without updating both
SDKs, one of these tests fails — that's the whole point.

Note on the original parity brief: the brief listed the asserted fields in
snake_case (``total_latency_ms``, ``estimated_cost_cents``, ...) and a flat
``timestamp``. The wire format both SDKs actually produce — and that the
ingest API accepts — is camelCase nested under ``metrics[]``, with
``windowStart`` / ``windowEnd`` (ISO-8601) instead of a single timestamp.
This test therefore asserts the *real* shape, but covers every field the
brief mentioned (``provider``, ``endpoint``, ``method``, ``totalLatencyMs``,
``estimatedCostCents``, plus the window timestamps) with the correct types
and units.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from recost._aggregator import Aggregator
from recost._types import RawEvent, WindowSummary


# ---------------------------------------------------------------------------
# Schema constants — keep in sync with tests/contract.test.ts
# ---------------------------------------------------------------------------

EXPECTED_TOP_LEVEL_KEYS = sorted([
    "projectId",
    "environment",
    "sdkLanguage",
    "sdkVersion",
    "windowStart",
    "windowEnd",
    "metrics",
])

EXPECTED_METRIC_KEYS = sorted([
    "provider",
    "endpoint",
    "method",
    "requestCount",
    "errorCount",
    "totalLatencyMs",
    "p50LatencyMs",
    "p95LatencyMs",
    "totalRequestBytes",
    "totalResponseBytes",
    "estimatedCostCents",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw_event(**overrides: Any) -> RawEvent:
    base = dict(
        timestamp="2026-04-21T12:00:00.000Z",
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        host="api.openai.com",
        path="/v1/chat/completions",
        status_code=200,
        latency_ms=120,
        request_bytes=100,
        response_bytes=500,
        provider="openai",
        endpoint_category="chat_completions",
        error=False,
    )
    base.update(overrides)
    return RawEvent(**base)


def _build_flush_payload() -> WindowSummary:
    """Mirror the _init.py path: ingest → flush → serialize."""
    aggregator = Aggregator(
        project_id="proj-contract",
        environment="test",
        sdk_version="0.1.0",
    )
    aggregator.ingest(_make_raw_event(latency_ms=100), 0.5)
    aggregator.ingest(_make_raw_event(latency_ms=300), 0.5)
    aggregator.ingest(_make_raw_event(method="GET", error=True, status_code=500), 0.0)

    summary = aggregator.flush()
    assert summary is not None, "aggregator.flush() returned None"
    return summary


def _on_wire(summary: WindowSummary) -> dict:
    """Round-trip through JSON the same way Transport does."""
    return json.loads(json.dumps(summary.to_dict()))


# ---------------------------------------------------------------------------
# Top-level WindowSummary contract
# ---------------------------------------------------------------------------


class TestWindowSummaryShape:
    def test_has_exactly_documented_top_level_fields(self):
        on_wire = _on_wire(_build_flush_payload())
        assert sorted(on_wire.keys()) == EXPECTED_TOP_LEVEL_KEYS

    def test_window_start_and_end_are_iso_strings(self):
        summary = _build_flush_payload()
        for value in (summary.window_start, summary.window_end):
            assert isinstance(value, str)
            # Guard against accidentally emitting unix-ms numbers, which the
            # API would reject. fromisoformat tolerates the ISO-8601 forms
            # the aggregator emits (datetime.now(timezone.utc).isoformat()).
            datetime.fromisoformat(value.replace("Z", "+00:00"))

    def test_identifies_itself_as_python_sdk(self):
        summary = _build_flush_payload()
        assert summary.sdk_language == "python"
        assert _on_wire(summary)["sdkLanguage"] == "python"

    def test_metrics_is_non_empty_list_for_non_empty_windows(self):
        on_wire = _on_wire(_build_flush_payload())
        assert isinstance(on_wire["metrics"], list)
        assert len(on_wire["metrics"]) > 0


# ---------------------------------------------------------------------------
# Per-MetricEntry contract — these are the fields the brief called out
# ---------------------------------------------------------------------------


class TestMetricEntryShape:
    def test_each_metric_has_exactly_documented_keys(self):
        on_wire = _on_wire(_build_flush_payload())
        for metric in on_wire["metrics"]:
            assert sorted(metric.keys()) == EXPECTED_METRIC_KEYS

    def test_provider_endpoint_method_are_strings_method_uppercase(self):
        on_wire = _on_wire(_build_flush_payload())
        for metric in on_wire["metrics"]:
            assert isinstance(metric["provider"], str)
            assert isinstance(metric["endpoint"], str)
            assert isinstance(metric["method"], str)
            assert metric["method"] == metric["method"].upper()

    def test_total_latency_ms_is_non_negative_integer(self):
        on_wire = _on_wire(_build_flush_payload())
        for metric in on_wire["metrics"]:
            v = metric["totalLatencyMs"]
            # Allow Python int but reject bool (which is an int subclass) and
            # floats — the API contract is integer milliseconds.
            assert isinstance(v, int) and not isinstance(v, bool)
            assert v >= 0
        # Sanity: aggregator summed 100 + 300 for the POST bucket
        post = next(m for m in on_wire["metrics"] if m["method"] == "POST")
        assert post["totalLatencyMs"] == 400

    def test_estimated_cost_cents_is_non_negative_number(self):
        on_wire = _on_wire(_build_flush_payload())
        for metric in on_wire["metrics"]:
            v = metric["estimatedCostCents"]
            # Cents may be fractional — accept either int or float, reject bool.
            assert isinstance(v, (int, float)) and not isinstance(v, bool)
            assert v >= 0
        post = next(m for m in on_wire["metrics"] if m["method"] == "POST")
        assert post["estimatedCostCents"] == pytest.approx(1.0)

    def test_counter_fields_are_non_negative_integers(self):
        on_wire = _on_wire(_build_flush_payload())
        for metric in on_wire["metrics"]:
            for key in (
                "requestCount",
                "errorCount",
                "totalRequestBytes",
                "totalResponseBytes",
                "p50LatencyMs",
                "p95LatencyMs",
            ):
                v = metric[key]
                assert isinstance(v, int) and not isinstance(v, bool), (
                    f"{key} must be int, got {type(v).__name__}"
                )
                assert v >= 0
