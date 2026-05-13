"""
Tests for recost/_interceptor.py

Tests urllib3 (via requests), httpx sync, httpx async interception.
"""

import sys
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
        if self.path == "/notfound":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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


def _find_free_port() -> int:
    """Bind an ephemeral TCP socket and immediately release it.

    The returned port is very likely free for ~µs after this call, which is
    enough for a single aiohttp connect attempt that we *want* to fail.
    """
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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


# ---------------------------------------------------------------------------
# Thread safety — install/uninstall race
# ---------------------------------------------------------------------------


class TestInstallUninstallRace:
    """Regression tests for issue #4 — install/uninstall must be safe under
    concurrent calls so the patch state cannot drift away from the
    ``_installed`` flag."""

    def test_concurrent_install_uninstall_leaves_consistent_state(self):
        """N threads each loop install/uninstall many times. After all
        threads finish, the interceptor must be uninstalled and no exception
        should have been raised."""
        from recost._interceptor import install, uninstall, is_installed

        iterations_per_thread = 200
        num_threads = 4
        exceptions: list[BaseException] = []

        def _noop(_event) -> None:
            pass

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    install(_noop)
                    uninstall()
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        # Force aggressive GIL yields so the race fires reliably on modern
        # CPython. Restored in finally.
        original_switch = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)
        try:
            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
        finally:
            sys.setswitchinterval(original_switch)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        assert not is_installed(), "interceptor should be uninstalled after the race"

    def test_concurrent_install_uninstall_does_not_leak_patches(self):
        """Pre-fix: two threads can both enter ``install()`` before either has
        set ``_installed = True``, both patch the target, and the second
        wraps the first. A subsequent ``uninstall()`` unwraps only the
        outer layer — the inner wrapper persists even after ``_installed``
        is False. This test verifies the original urllib3 callable is
        restored after a concurrent install/uninstall race."""
        try:
            import urllib3
        except ImportError:
            import pytest

            pytest.skip("urllib3 not installed")

        from recost._interceptor import install, uninstall, is_installed

        original_urlopen = urllib3.HTTPConnectionPool.urlopen

        def _noop(_event) -> None:
            pass

        iterations_per_thread = 200
        num_threads = 4
        exceptions: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(iterations_per_thread):
                    install(_noop)
                    uninstall()
            except BaseException as exc:  # noqa: BLE001
                exceptions.append(exc)

        original_switch = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)
        try:
            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
        finally:
            sys.setswitchinterval(original_switch)

        assert not exceptions, f"unexpected exceptions: {exceptions!r}"
        assert not is_installed(), "interceptor should be uninstalled"
        # The critical invariant: urllib3's method is back to its real
        # original, not a recost wrapper.
        assert urllib3.HTTPConnectionPool.urlopen is original_urlopen, (
            "concurrent install/uninstall left a recost wrapper in place"
        )


# ---------------------------------------------------------------------------
# aiohttp body sizing — json= and FormData
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aiohttp_json_body_records_size(mock_http_server_200) -> None:  # type: ignore[no-untyped-def]
    """When the user passes `json={...}` to aiohttp, request_bytes must reflect
    the serialized JSON length, not 0."""
    import aiohttp
    import json as _json
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(mock_http_server_200.url, json={"x": "y"}) as resp:
                await resp.read()
        assert len(events) >= 1
        ev = events[-1]
        expected = len(_json.dumps({"x": "y"}))
        assert ev.request_bytes == expected
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_aiohttp_formdata_body_records_size(mock_http_server_200) -> None:  # type: ignore[no-untyped-def]
    """aiohttp FormData payloads must report non-zero request_bytes."""
    import aiohttp
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)
    try:
        form = aiohttp.FormData()
        form.add_field("name", "value")
        async with aiohttp.ClientSession() as session:
            async with session.post(mock_http_server_200.url, data=form) as resp:
                await resp.read()
        ev = events[-1]
        assert ev.request_bytes > 0
    finally:
        uninstall()


def test_httpx_streaming_content_not_materialized(mock_http_server_200) -> None:  # type: ignore[no-untyped-def]
    """Passing an async-iterable body to httpx must not be read by the interceptor.
    The crucial assertion: request_bytes is 0 (we skipped sizing the stream) and
    the SDK does not crash."""
    import httpx
    from recost._interceptor import install, uninstall

    events: list = []
    install(events.append)

    def stream_body():
        yield b"chunk1"
        yield b"chunk2"

    try:
        req = httpx.Request("POST", mock_http_server_200.url, content=stream_body())
        with httpx.Client() as client:
            client.send(req)
        ev = events[-1]
        assert ev.request_bytes == 0
    finally:
        uninstall()
