"""Tests for ecoapi/_types.py — serialization to camelCase."""

from ecoapi import MetricEntry, WindowSummary


def test_metric_entry_to_dict_camel_case():
    entry = MetricEntry(
        provider="openai",
        endpoint="chat_completions",
        method="POST",
        request_count=5,
        error_count=1,
        total_latency_ms=2500,
        p50_latency_ms=400,
        p95_latency_ms=800,
        total_request_bytes=5000,
        total_response_bytes=10000,
        estimated_cost_cents=10.0,
    )
    d = entry.to_dict()
    assert d["provider"] == "openai"
    assert d["requestCount"] == 5
    assert d["errorCount"] == 1
    assert d["totalLatencyMs"] == 2500
    assert d["p50LatencyMs"] == 400
    assert d["p95LatencyMs"] == 800
    assert d["totalRequestBytes"] == 5000
    assert d["totalResponseBytes"] == 10000
    assert d["estimatedCostCents"] == 10.0


def test_window_summary_to_dict_camel_case():
    entry = MetricEntry(
        provider="stripe",
        endpoint="charges",
        method="POST",
        request_count=1,
        error_count=0,
        total_latency_ms=100,
        p50_latency_ms=100,
        p95_latency_ms=100,
        total_request_bytes=200,
        total_response_bytes=400,
        estimated_cost_cents=0,
    )
    summary = WindowSummary(
        project_id="proj_123",
        environment="production",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-03-10T00:00:00.000Z",
        window_end="2026-03-10T00:00:30.000Z",
        metrics=[entry],
    )
    d = summary.to_dict()
    assert d["projectId"] == "proj_123"
    assert d["environment"] == "production"
    assert d["sdkLanguage"] == "python"
    assert d["sdkVersion"] == "0.1.0"
    assert d["windowStart"] == "2026-03-10T00:00:00.000Z"
    assert d["windowEnd"] == "2026-03-10T00:00:30.000Z"
    assert len(d["metrics"]) == 1
    assert d["metrics"][0]["provider"] == "stripe"


def test_window_summary_empty_metrics():
    summary = WindowSummary(
        project_id="",
        environment="dev",
        sdk_language="python",
        sdk_version="0.0.0",
        window_start="",
        window_end="",
    )
    d = summary.to_dict()
    assert d["metrics"] == []
