"""
Tests for recost/_transport.py
"""

import asyncio
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from recost._transport import Transport, _LocalTransport
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


# ---------------------------------------------------------------------------
# Rejection signalling (422, on_error, warnings, last_flush_status)
# ---------------------------------------------------------------------------


def _make_metric(**overrides) -> MetricEntry:
    defaults = dict(
        provider="openai",
        endpoint="chat_completions",
        method="POST",
        request_count=1,
        error_count=0,
        total_latency_ms=100,
        p50_latency_ms=100,
        p95_latency_ms=100,
        total_request_bytes=10,
        total_response_bytes=20,
        estimated_cost_cents=1.0,
    )
    defaults.update(overrides)
    return MetricEntry(**defaults)


def _make_summary_with_metrics(metrics) -> WindowSummary:
    return WindowSummary(
        project_id="p",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-01T00:00:30Z",
        metrics=metrics,
    )


class TestRejectionSignalling:
    def test_422_fires_on_error_with_descriptive_message(self, cloud_server):
        base_url, _ = cloud_server
        _CloudHandler.response_code = 422

        errors: list[Exception] = []
        config = RecostConfig(
            api_key="k",
            project_id="p",
            base_url=base_url,
            max_retries=0,
            on_error=lambda e: errors.append(e),
            debug=False,
        )
        transport = Transport(config)
        transport.send(_make_summary_with_metrics([_make_metric(), _make_metric(endpoint="b")]))
        status = transport.last_flush_status
        transport.dispose()

        assert len(errors) == 1
        assert "422" in str(errors[0])
        assert "windowSize=2" in str(errors[0])
        assert status is not None
        assert status.status == "error"
        assert status.window_size == 2

    def test_422_logs_warning_when_debug_false(self, cloud_server, caplog):
        base_url, _ = cloud_server
        _CloudHandler.response_code = 422

        config = RecostConfig(
            api_key="k",
            project_id="p",
            base_url=base_url,
            max_retries=0,
            debug=False,
        )
        transport = Transport(config)
        with caplog.at_level(logging.WARNING, logger="recost"):
            transport.send(_make_summary_with_metrics([_make_metric()]))
        transport.dispose()

        rejection_records = [r for r in caplog.records if "HTTP 422" in r.getMessage()]
        assert len(rejection_records) >= 1
        assert "windowSize=1" in rejection_records[0].getMessage()

    def test_last_flush_status_ok_on_success(self, cloud_server):
        base_url, _ = cloud_server
        _CloudHandler.response_code = 202
        config = RecostConfig(api_key="k", project_id="p", base_url=base_url, max_retries=0)
        transport = Transport(config)
        transport.send(_make_summary_with_metrics([_make_metric()]))
        status = transport.last_flush_status
        transport.dispose()

        assert status is not None
        assert status.status == "ok"
        assert status.window_size == 1
        assert isinstance(status.timestamp, int)

    def test_summaries_larger_than_max_buckets_are_chunked(self, cloud_server):
        base_url, _ = cloud_server
        _CloudHandler.response_code = 202
        config = RecostConfig(
            api_key="k",
            project_id="p",
            base_url=base_url,
            max_retries=0,
            max_buckets=3,
        )
        transport = Transport(config)
        metrics = [_make_metric(endpoint=f"ep{i}") for i in range(7)]
        transport.send(_make_summary_with_metrics(metrics))
        status = transport.last_flush_status
        transport.dispose()

        assert len(_CloudHandler.received) == 3  # ceil(7/3)
        sizes = [len(r["body"]["metrics"]) for r in _CloudHandler.received]
        assert sizes == [3, 3, 1]
        assert status is not None
        assert status.status == "ok"
        assert status.window_size == 1  # final chunk


# ---------------------------------------------------------------------------
# Local WebSocket transport — concurrency safety
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestLocalTransportSync:
    """Sync-path invariants: send() must never block, dispose() must always
    unblock the loop thread, even when no WebSocket server is listening."""

    def test_send_does_not_block_caller_without_server(self):
        pytest.importorskip("websockets")
        port = _find_free_port()
        t = _LocalTransport(port=port)
        try:
            start = time.monotonic()
            for _ in range(200):
                t.send("payload")
            elapsed = time.monotonic() - start
            assert elapsed < 0.5, f"send() blocked for {elapsed:.2f}s"
        finally:
            t.dispose()

    def test_dispose_joins_cleanly_without_server(self):
        pytest.importorskip("websockets")
        port = _find_free_port()
        t = _LocalTransport(port=port)
        # Give the loop a moment to start and attempt its first connect
        time.sleep(0.2)
        start = time.monotonic()
        t.dispose()
        elapsed = time.monotonic() - start
        assert t._thread is None
        # Loop yields every 500ms, so dispose should return well under 2s
        assert elapsed < 2.5, f"dispose() took {elapsed:.2f}s — loop was wedged"

    def test_send_after_dispose_is_noop(self):
        """Calling send() post-dispose must not raise (loop may be closed)."""
        pytest.importorskip("websockets")
        port = _find_free_port()
        t = _LocalTransport(port=port)
        t.dispose()
        t.send("late-payload")  # must not raise

    def test_transport_local_mode_dispose_is_fast(self):
        """Regression: the full Transport wrapper in local mode disposes
        within the loop-yield window — no blocking queue.get hanging."""
        pytest.importorskip("websockets")
        port = _find_free_port()
        transport = Transport(RecostConfig(local_port=port))
        assert transport.mode == "local"
        time.sleep(0.2)
        start = time.monotonic()
        transport.dispose()
        assert time.monotonic() - start < 2.5


class TestLocalTransportAsync:
    """End-to-end: real WS server, exercises the reconnect path that was
    prone to deadlocking when the drain loop used a blocking queue.get()."""

    async def test_reconnect_after_ws_drop_does_not_deadlock(self):
        websockets = pytest.importorskip("websockets")
        port = _find_free_port()

        received: list[str] = []
        connect_count = [0]

        async def handler(ws):
            connect_count[0] += 1
            my_conn = connect_count[0]
            try:
                async for msg in ws:
                    received.append(msg)
                    # Drop the first connection after its first message to
                    # force the transport's reconnect path.
                    if my_conn == 1:
                        await ws.close()
                        return
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", port)
        t = _LocalTransport(port=port)
        try:
            # Wait for initial connection
            for _ in range(50):
                if connect_count[0] >= 1:
                    break
                await asyncio.sleep(0.05)
            assert connect_count[0] >= 1, "transport never connected"

            t.send("msg-1")
            for _ in range(100):
                if len(received) >= 1:
                    break
                await asyncio.sleep(0.05)
            assert len(received) == 1

            # Give the server's close() a beat to propagate to the client
            await asyncio.sleep(0.1)

            # This send happens after the drop. The transport should re-queue
            # it, reconnect, and deliver it on the new connection.
            t.send("msg-2")
            for _ in range(200):
                if len(received) >= 2:
                    break
                await asyncio.sleep(0.05)

            assert len(received) >= 2, f"reconnect failed, received={received}"
            assert connect_count[0] >= 2, "transport did not reconnect"

            # Dispose must still exit promptly even after a reconnect cycle
            start = time.monotonic()
            t.dispose()
            assert time.monotonic() - start < 2.5
        finally:
            if t._thread is not None:
                t.dispose()
            server.close()
            await server.wait_closed()

    async def test_send_from_running_event_loop_does_not_block(self):
        """When the caller is already inside an asyncio loop (e.g. an async
        web framework), send() must not block that loop. The transport's
        own loop lives on a separate thread — this verifies the bridge."""
        pytest.importorskip("websockets")
        port = _find_free_port()
        t = _LocalTransport(port=port)
        try:
            start = time.monotonic()
            for _ in range(100):
                t.send("x")
                # Yield so any accidental blocking would be visible
                await asyncio.sleep(0)
            elapsed = time.monotonic() - start
            assert elapsed < 0.5
        finally:
            t.dispose()


# ---------------------------------------------------------------------------
# Thread safety — dispose during WebSocket connect must not leak the thread
# ---------------------------------------------------------------------------


class TestDisposeDuringConnect:
    """Regression for issue #6 — when dispose() lands while the loop is inside
    websockets.connect()'s blocking TCP+upgrade handshake, the old sentinel-
    in-queue dispose path could leave the daemon thread + socket FD pinned
    until the OS TCP timeout (~75 s). The fix stops the loop directly."""

    @staticmethod
    def _start_blackhole_server(port: int) -> socket.socket:
        """Bind+listen on `port` and accept connections but never respond
        to the HTTP upgrade. websockets.connect() will TCP-connect, send its
        upgrade request, and then block waiting for an HTTP response."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        accepted: list[socket.socket] = []

        def accept_loop() -> None:
            srv.settimeout(0.5)
            while True:
                try:
                    conn, _ = srv.accept()
                    accepted.append(conn)
                except (OSError, socket.timeout):
                    if srv.fileno() == -1:
                        return

        threading.Thread(target=accept_loop, daemon=True).start()
        return srv

    def test_dispose_during_connect_returns_promptly(self):
        pytest.importorskip("websockets")
        port = _find_free_port()
        server = self._start_blackhole_server(port)
        try:
            t = _LocalTransport(port=port)
            # Give the transport a tick to enter websockets.connect's
            # post-TCP-accept handshake wait.
            time.sleep(0.5)
            start = time.monotonic()
            t.dispose()
            elapsed = time.monotonic() - start
            assert t._thread is None, "transport thread did not join"
            # Pre-fix this took ~75s on Linux (OS TCP timeout) or ~2.5s on
            # Windows (the old 2s join + dispose overhead). Post-fix the
            # loop.stop() path returns in well under a second. The 1.5s
            # threshold catches a regression to the queue-sentinel design
            # while leaving headroom for slow CI hosts.
            assert elapsed < 1.5, (
                f"dispose() took {elapsed:.2f}s — the loop did not stop "
                f"while inside websockets.connect()"
            )
        finally:
            server.close()


# ---------------------------------------------------------------------------
# _CloudResult / _post_cloud tests
# ---------------------------------------------------------------------------


def test_post_cloud_returns_status_on_2xx(mock_http_server_200) -> None:  # type: ignore[no-untyped-def]
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_200.url,
        body='{"ping": 1}',
        api_key="rc-test",
        max_retries=0,
    )
    assert result.status == 200
    assert result.retry_after_ms is None
    assert result.error is None


def test_post_cloud_returns_401_status_without_raising(mock_http_server_401) -> None:  # type: ignore[no-untyped-def]
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_401.url,
        body='{"ping": 1}',
        api_key="rc-bad",
        max_retries=0,
    )
    assert result.status == 401
    assert result.retry_after_ms is None


def test_post_cloud_parses_retry_after_seconds_on_429(mock_http_server_429_retry_after_2) -> None:  # type: ignore[no-untyped-def]
    from recost._transport import _post_cloud
    result = _post_cloud(
        url=mock_http_server_429_retry_after_2.url,
        body='{"ping": 1}',
        api_key="rc-ok",
        max_retries=0,
    )
    assert result.status == 429
    assert result.retry_after_ms == 2000


def test_first_401_emits_recost_auth_error(mock_http_server_401) -> None:  # type: ignore[no-untyped-def]
    from recost import RecostAuthError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    transport = Transport(RecostConfig(
        api_key="rc-bad",
        project_id="p_1",
        base_url=mock_http_server_401.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert len(errors) == 1
    assert isinstance(errors[0], RecostAuthError)
    assert errors[0].status == 401
    assert errors[0].consecutive_failures == 1


def test_fifth_consecutive_401_emits_fatal_and_suspends(mock_http_server_401) -> None:  # type: ignore[no-untyped-def]
    from recost import RecostFatalAuthError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    transport = Transport(RecostConfig(
        api_key="rc-bad",
        project_id="p_1",
        base_url=mock_http_server_401.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    for _ in range(5):
        transport.send(summary)
    fatal = [e for e in errors if isinstance(e, RecostFatalAuthError)]
    assert len(fatal) == 1
    errors_before_sixth = list(errors)
    transport.send(summary)
    assert errors == errors_before_sixth  # nothing new appended


def test_429_emits_rate_limit_error_and_invokes_defer(mock_http_server_429_retry_after_2) -> None:  # type: ignore[no-untyped-def]
    from recost import RecostRateLimitError, RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    errors: list = []
    defers: list = []
    transport = Transport(RecostConfig(
        api_key="rc-ok",
        project_id="p_1",
        base_url=mock_http_server_429_retry_after_2.url.rstrip("/"),
        on_error=errors.append,
        max_retries=0,
    ))
    transport.set_defer_callback(defers.append)
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert len(errors) == 1
    assert isinstance(errors[0], RecostRateLimitError)
    assert errors[0].retry_after_ms == 2000
    assert defers == [2000]


def test_429_does_not_increment_auth_counter(mock_http_server_429_retry_after_2) -> None:  # type: ignore[no-untyped-def]
    from recost import RecostConfig
    from recost._transport import Transport
    from recost._types import WindowSummary

    transport = Transport(RecostConfig(
        api_key="rc-ok",
        project_id="p_1",
        base_url=mock_http_server_429_retry_after_2.url.rstrip("/"),
        max_retries=0,
    ))
    summary = WindowSummary(
        project_id="p_1",
        environment="test",
        sdk_language="python",
        sdk_version="0.1.0",
        window_start="2026-05-13T00:00:00Z",
        window_end="2026-05-13T00:00:30Z",
        metrics=[],
    )
    transport.send(summary)
    assert transport._consecutive_auth_failures == 0


# ---------------------------------------------------------------------------
# Self-instrumentation (#9) — cloud transport must not trigger urllib3 patch
# ---------------------------------------------------------------------------


class TestSelfInstrumentation:
    """The cloud transport uses urllib.request (stdlib) so the interceptor's
    urllib3 patch never sees its own POSTs. If a future change starts using
    requests/httpx for the cloud POST, the SDK would loop on itself —
    every flush would generate one more event to flush."""

    def test_cloud_flush_does_not_trigger_urllib3_patch(self, cloud_server):
        from recost._interceptor import install, uninstall
        from recost._transport import _post_cloud
        events: list = []
        base_url, _ = cloud_server
        install(events.append)
        try:
            result = _post_cloud(
                url=f"{base_url}/projects/proj/telemetry",
                body='{"ping": 1}',
                api_key="rc-test",
                max_retries=0,
            )
            assert 200 <= result.status < 300, (
                f"cloud_server should return 202, got {result.status}"
            )
            assert events == [], (
                f"cloud transport leaked into interceptor — captured {len(events)} "
                f"event(s); the urllib.request-based path must not be patched"
            )
        finally:
            uninstall()
