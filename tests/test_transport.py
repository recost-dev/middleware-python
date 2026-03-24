"""
Tests for recost/_transport.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from recost._transport import Transport, _post_cloud
from recost._types import RecostConfig, MetricEntry, WindowSummary


def _make_summary() -> WindowSummary:
    return WindowSummary(
        project_id="test-proj",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-03-10T00:00:00Z",
        window_end="2026-03-10T00:00:30Z",
        metrics=[
            MetricEntry(
                provider="openai",
                endpoint="chat_completions",
                method="POST",
                request_count=1,
                error_count=0,
                total_latency_ms=100,
                p50_latency_ms=100,
                p95_latency_ms=100,
                total_request_bytes=200,
                total_response_bytes=400,
                estimated_cost_cents=2.0,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Simple test server for cloud transport
# ---------------------------------------------------------------------------


class _CloudHandler(BaseHTTPRequestHandler):
    received = []
    response_code = 202

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        _CloudHandler.received.append({
            "path": self.path,
            "body": json.loads(body),
            "auth": self.headers.get("Authorization"),
        })
        self.send_response(_CloudHandler.response_code)
        self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def cloud_server():
    _CloudHandler.received = []
    _CloudHandler.response_code = 202
    server = HTTPServer(("127.0.0.1", 0), _CloudHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", port
    server.shutdown()


# ---------------------------------------------------------------------------
# Transport mode detection
# ---------------------------------------------------------------------------

class TestTransportMode:
    def test_cloud_mode_when_api_key_present(self):
        t = Transport(RecostConfig(api_key="test-key"))
        assert t.mode == "cloud"
        t.dispose()

    def test_local_mode_when_no_api_key(self):
        t = Transport(RecostConfig())
        assert t.mode == "local"
        t.dispose()


# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------

class TestCloudTransport:
    def test_sends_post_to_correct_url(self, cloud_server):
        base_url, _ = cloud_server
        config = RecostConfig(
            api_key="test-key",
            project_id="proj-123",
            base_url=base_url,
        )
        transport = Transport(config)
        summary = _make_summary()
        transport.send(summary)
        transport.dispose()

        assert len(_CloudHandler.received) == 1
        req = _CloudHandler.received[0]
        assert req["path"] == "/projects/proj-123/telemetry"
        assert req["auth"] == "Bearer test-key"
        assert req["body"]["projectId"] == "test-proj"
        assert req["body"]["sdkLanguage"] == "python"

    def test_no_retry_on_4xx(self, cloud_server):
        base_url, _ = cloud_server
        _CloudHandler.response_code = 400
        config = RecostConfig(
            api_key="test-key",
            project_id="proj-123",
            base_url=base_url,
            max_retries=3,
        )
        transport = Transport(config)
        transport.send(_make_summary())
        transport.dispose()
        # Should only have 1 request (no retries)
        assert len(_CloudHandler.received) == 1
