"""
Tests for recost/_interceptor.py

Tests urllib3 (via requests), httpx sync, httpx async interception.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from recost._interceptor import install, is_installed, uninstall
from recost._types import RawEvent


# ---------------------------------------------------------------------------
# Simple test HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_length)
        self.send_response(201)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"Created")

    def log_message(self, format, *args):
        pass  # Suppress server logging


@pytest.fixture(scope="module")
def test_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_not_installed_by_default(self):
        assert is_installed() is False

    def test_install_and_uninstall(self):
        install(lambda e: None)
        assert is_installed() is True
        uninstall()
        assert is_installed() is False

    def test_double_install_is_noop(self):
        events = []
        install(lambda e: events.append(e))
        install(lambda e: None)  # Should be a no-op
        assert is_installed() is True
        uninstall()
        assert is_installed() is False


# ---------------------------------------------------------------------------
# urllib3 / requests interception
# ---------------------------------------------------------------------------

class TestUrllib3:
    def test_captures_get(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        try:
            import requests
            resp = requests.get(f"{test_server}/test")
            assert resp.status_code == 200
            assert len(events) >= 1
            event = events[-1]
            assert event.method == "GET"
            assert "/test" in event.url
            assert event.status_code == 200
            assert event.latency_ms >= 0
        finally:
            uninstall()

    def test_captures_post(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        try:
            import requests
            resp = requests.post(f"{test_server}/submit", data=b"hello")
            assert resp.status_code == 201
            assert len(events) >= 1
            event = events[-1]
            assert event.method == "POST"
            assert event.status_code == 201
        finally:
            uninstall()

    def test_strips_query_params(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        try:
            import requests
            requests.get(f"{test_server}/path?secret=key&token=abc")
            assert len(events) >= 1
            event = events[-1]
            assert "?" not in event.url
            assert "secret" not in event.url
        finally:
            uninstall()


# ---------------------------------------------------------------------------
# httpx sync interception
# ---------------------------------------------------------------------------

class TestHttpxSync:
    def test_captures_get(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        try:
            import httpx
            with httpx.Client() as client:
                resp = client.get(f"{test_server}/httpx-test")
            assert resp.status_code == 200
            assert len(events) >= 1
            event = events[-1]
            assert event.method == "GET"
            assert "/httpx-test" in event.url
            assert event.status_code == 200
        finally:
            uninstall()


# ---------------------------------------------------------------------------
# httpx async interception
# ---------------------------------------------------------------------------

class TestHttpxAsync:
    @pytest.mark.asyncio
    async def test_captures_async_get(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{test_server}/async-test")
            assert resp.status_code == 200
            assert len(events) >= 1
            event = events[-1]
            assert event.method == "GET"
            assert "/async-test" in event.url
        finally:
            uninstall()


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

class TestSafety:
    def test_callback_exception_does_not_break_request(self, test_server):
        def bad_callback(event: RawEvent):
            raise RuntimeError("boom")

        install(bad_callback)
        try:
            import requests
            resp = requests.get(f"{test_server}/safe")
            assert resp.status_code == 200
        finally:
            uninstall()

    def test_uninstall_restores_originals(self, test_server):
        events: list[RawEvent] = []
        install(lambda e: events.append(e))
        uninstall()

        import requests
        requests.get(f"{test_server}/after-uninstall")
        assert len(events) == 0
