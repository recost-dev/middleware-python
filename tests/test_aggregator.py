"""
Tests for ecoapi/_aggregator.py

Ported from the Node SDK's aggregator.test.ts.
"""

from datetime import datetime, timezone

from ecoapi._aggregator import Aggregator
from ecoapi._types import RawEvent


def make_event(**overrides) -> RawEvent:
    defaults = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        host="api.openai.com",
        path="/v1/chat/completions",
        status_code=200,
        latency_ms=500,
        request_bytes=1000,
        response_bytes=2000,
        provider="openai",
        endpoint_category="chat_completions",
        error=False,
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


# ---------------------------------------------------------------------------
# Basic flush behavior
# ---------------------------------------------------------------------------

class TestBasicFlush:
    def test_flush_returns_none_when_empty(self):
        agg = Aggregator()
        assert agg.flush() is None

    def test_flush_returns_summary_after_one_event(self):
        agg = Aggregator(project_id="p1", environment="test")
        agg.ingest(make_event(latency_ms=250, request_bytes=512, response_bytes=1024), 2.0)
        summary = agg.flush()
        assert summary is not None
        assert len(summary.metrics) == 1
        entry = summary.metrics[0]
        assert entry.request_count == 1
        assert entry.error_count == 0
        assert entry.total_latency_ms == 250
        assert entry.p50_latency_ms == 250
        assert entry.p95_latency_ms == 250
        assert entry.total_request_bytes == 512
        assert entry.total_response_bytes == 1024
        assert entry.estimated_cost_cents == 2.0

    def test_flush_resets_state(self):
        agg = Aggregator()
        for _ in range(5):
            agg.ingest(make_event())
        agg.flush()
        assert agg.size == 0
        assert agg.bucket_count == 0
        for _ in range(3):
            agg.ingest(make_event())
        summary = agg.flush()
        assert summary is not None
        assert summary.metrics[0].request_count == 3

    def test_double_flush_returns_none(self):
        agg = Aggregator()
        agg.ingest(make_event())
        agg.flush()
        assert agg.flush() is None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_groups_by_provider_endpoint_method(self):
        agg = Aggregator()
        agg.ingest(make_event(provider="openai", endpoint_category="chat_completions", method="POST"))
        agg.ingest(make_event(provider="openai", endpoint_category="embeddings", method="POST"))
        agg.ingest(make_event(provider="stripe", endpoint_category="charges", method="POST"))
        summary = agg.flush()
        assert summary is not None
        assert len(summary.metrics) == 3
        for entry in summary.metrics:
            assert entry.request_count == 1

    def test_combines_same_group(self):
        agg = Aggregator()
        latencies = [100, 200, 300, 400, 500]
        for ms in latencies:
            agg.ingest(make_event(latency_ms=ms, request_bytes=10, response_bytes=20), 1.0)
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        assert entry.request_count == 5
        assert entry.total_latency_ms == 1500
        assert entry.estimated_cost_cents == 5.0
        assert entry.total_request_bytes == 50
        assert entry.total_response_bytes == 100

    def test_counts_errors(self):
        agg = Aggregator()
        for i in range(10):
            agg.ingest(make_event(error=i < 3, status_code=500 if i < 3 else 200))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        assert entry.request_count == 10
        assert entry.error_count == 3

    def test_sums_bytes(self):
        agg = Aggregator()
        agg.ingest(make_event(request_bytes=100, response_bytes=400))
        agg.ingest(make_event(request_bytes=200, response_bytes=500))
        agg.ingest(make_event(request_bytes=300, response_bytes=600))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        assert entry.total_request_bytes == 600
        assert entry.total_response_bytes == 1500

    def test_sums_cost(self):
        agg = Aggregator()
        for _ in range(5):
            agg.ingest(make_event(), 1.5)
        summary = agg.flush()
        assert summary is not None
        assert abs(summary.metrics[0].estimated_cost_cents - 7.5) < 0.001

    def test_cost_defaults_to_zero(self):
        agg = Aggregator()
        agg.ingest(make_event())
        summary = agg.flush()
        assert summary is not None
        assert summary.metrics[0].estimated_cost_cents == 0


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

class TestPercentiles:
    def test_single_event(self):
        agg = Aggregator()
        agg.ingest(make_event(latency_ms=42))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        assert entry.p50_latency_ms == 42
        assert entry.p95_latency_ms == 42

    def test_two_events(self):
        agg = Aggregator()
        agg.ingest(make_event(latency_ms=900))
        agg.ingest(make_event(latency_ms=100))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        # Sorted: [100, 900]
        # p50: ceil(2*0.5)-1 = 0 → 100
        assert entry.p50_latency_ms == 100
        # p95: ceil(2*0.95)-1 = 1 → 900
        assert entry.p95_latency_ms == 900

    def test_five_events(self):
        agg = Aggregator()
        for ms in [100, 200, 300, 400, 500]:
            agg.ingest(make_event(latency_ms=ms))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        # ceil(5*0.5)-1 = 2 → 300
        assert entry.p50_latency_ms == 300
        # ceil(5*0.95)-1 = 4 → 500
        assert entry.p95_latency_ms == 500

    def test_hundred_events(self):
        agg = Aggregator()
        for i in range(1, 101):
            agg.ingest(make_event(latency_ms=i))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        # ceil(100*0.5)-1 = 49 → 50
        assert entry.p50_latency_ms == 50
        # ceil(100*0.95)-1 = 94 → 95
        assert entry.p95_latency_ms == 95

    def test_identical_values(self):
        agg = Aggregator()
        for _ in range(10):
            agg.ingest(make_event(latency_ms=42))
        summary = agg.flush()
        assert summary is not None
        entry = summary.metrics[0]
        assert entry.p50_latency_ms == 42
        assert entry.p95_latency_ms == 42


# ---------------------------------------------------------------------------
# Null provider handling
# ---------------------------------------------------------------------------

class TestNullProviderHandling:
    def test_null_provider_becomes_unknown(self):
        agg = Aggregator()
        agg.ingest(make_event(provider=None, endpoint_category="something"))
        summary = agg.flush()
        assert summary is not None
        assert summary.metrics[0].provider == "unknown"

    def test_null_endpoint_uses_raw_path(self):
        agg = Aggregator()
        agg.ingest(make_event(endpoint_category=None, path="/api/internal"))
        summary = agg.flush()
        assert summary is not None
        assert summary.metrics[0].endpoint == "/api/internal"

    def test_both_null(self):
        agg = Aggregator()
        agg.ingest(make_event(provider=None, endpoint_category=None, path="/v1/unknown"))
        summary = agg.flush()
        assert summary is not None
        assert summary.metrics[0].provider == "unknown"
        assert summary.metrics[0].endpoint == "/v1/unknown"


# ---------------------------------------------------------------------------
# Window timestamps
# ---------------------------------------------------------------------------

class TestWindowTimestamps:
    def test_window_start_matches_first_event(self):
        agg = Aggregator()
        t1 = "2026-03-10T00:00:00.000Z"
        agg.ingest(make_event(timestamp=t1))
        agg.ingest(make_event(timestamp="2026-03-10T00:00:01.000Z"))
        summary = agg.flush()
        assert summary is not None
        assert summary.window_start == t1

    def test_window_end_is_approximately_now(self):
        agg = Aggregator()
        agg.ingest(make_event())
        before = datetime.now(timezone.utc)
        summary = agg.flush()
        after = datetime.now(timezone.utc)
        assert summary is not None
        window_end = datetime.fromisoformat(summary.window_end.replace("Z", "+00:00"))
        # Allow some slack
        assert window_end >= before.replace(microsecond=0)

    def test_window_end_after_start(self):
        agg = Aggregator()
        agg.ingest(make_event(timestamp="2020-01-01T00:00:00.000Z"))
        summary = agg.flush()
        assert summary is not None
        start = datetime.fromisoformat(summary.window_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(summary.window_end.replace("Z", "+00:00"))
        assert end >= start


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_includes_constructor_config(self):
        agg = Aggregator(project_id="proj_123", environment="production", sdk_version="1.2.3")
        agg.ingest(make_event())
        summary = agg.flush()
        assert summary is not None
        assert summary.project_id == "proj_123"
        assert summary.environment == "production"
        assert summary.sdk_version == "1.2.3"
        assert summary.sdk_language == "python"

    def test_defaults(self):
        agg = Aggregator()
        agg.ingest(make_event())
        summary = agg.flush()
        assert summary is not None
        assert summary.project_id == ""
        assert summary.environment == "development"
        assert summary.sdk_version == "0.0.0"


# ---------------------------------------------------------------------------
# Size and bucket count
# ---------------------------------------------------------------------------

class TestSizeAndBucketCount:
    def test_size_tracks_total(self):
        agg = Aggregator()
        agg.ingest(make_event())
        agg.ingest(make_event())
        agg.ingest(make_event())
        assert agg.size == 3

    def test_bucket_count_tracks_groups(self):
        agg = Aggregator()
        agg.ingest(make_event(provider="openai", endpoint_category="chat_completions"))
        agg.ingest(make_event(provider="openai", endpoint_category="chat_completions"))
        agg.ingest(make_event(provider="stripe", endpoint_category="charges"))
        agg.ingest(make_event(provider="stripe", endpoint_category="charges"))
        agg.ingest(make_event(provider="stripe", endpoint_category="charges"))
        assert agg.size == 5
        assert agg.bucket_count == 2

    def test_reset_after_flush(self):
        agg = Aggregator()
        agg.ingest(make_event())
        agg.ingest(make_event())
        agg.flush()
        assert agg.size == 0
        assert agg.bucket_count == 0

    def test_fresh_instance_zeros(self):
        agg = Aggregator()
        assert agg.size == 0
        assert agg.bucket_count == 0

    def test_large_batch(self):
        agg = Aggregator()
        providers = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
        for i in range(1000):
            p = providers[i % 10]
            agg.ingest(make_event(provider=p, endpoint_category=p))
        summary = agg.flush()
        assert summary is not None
        assert len(summary.metrics) == 10
        for entry in summary.metrics:
            assert entry.request_count == 100
