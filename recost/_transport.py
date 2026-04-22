"""
Transport — delivers WindowSummary payloads to either:
  - api.recost.dev (cloud mode) via HTTPS POST with exponential-backoff retry, or
  - the ReCost VS Code extension (local mode) via WebSocket on localhost.

Uses urllib.request (stdlib) for cloud transport to avoid self-instrumentation
(the interceptor patches urllib3, not urllib.request).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Optional, Tuple

from ._aggregator import MAX_BUCKETS
from ._types import FlushStatus, RecostConfig, TransportMode, WindowSummary

logger = logging.getLogger("recost")

# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------


def _post_cloud(
    url: str,
    body: str,
    api_key: str,
    max_retries: int,
) -> Tuple[bool, int, Optional[str]]:
    """POST a JSON body to the cloud API with retry.

    Returns (ok, status, request_id):
      - (True, status, req_id)  on 2xx
      - (False, status, req_id) on 4xx (caller decides how to surface)
    The response body is intentionally never read — 4xx bodies can echo
    field-level validation detail that hints at project/key shape, and we
    don't want any of that landing in user logs. ``x-request-id`` is the
    one piece of response metadata the caller is allowed to log.

    Raises the last exception when retries on 5xx / network errors are
    exhausted.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "recost-python/0.1.0",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=30)
            status = resp.getcode() or 0
            request_id = resp.headers.get("x-request-id") if resp.headers else None
            if 200 <= status < 300:
                return True, status, request_id
            # 4xx errors are not retriable
            if 400 <= status < 500:
                return False, status, request_id
            last_error = Exception(f"HTTP {status}")
        except urllib.error.HTTPError as e:
            request_id = e.headers.get("x-request-id") if e.headers else None
            if 400 <= e.code < 500:
                return False, e.code, request_id
            last_error = e
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            time.sleep(min(1.0 * (2 ** attempt), 10.0))

    if last_error is not None:
        raise last_error
    return False, 0, None


# ---------------------------------------------------------------------------
# Local WebSocket transport
# ---------------------------------------------------------------------------


class _LocalTransport:
    """Background-thread WebSocket transport for local mode.

    Concurrency model: a dedicated daemon thread owns an asyncio event loop.
    The loop drains an asyncio.Queue via `await asyncio.wait_for(..., timeout=0.5)`,
    which yields control to the loop every 500 ms so WebSocket keepalive /
    close frames are never starved. The public sync `send()` hands payloads
    to that queue from any thread via `run_coroutine_threadsafe` — it never
    blocks the caller and never blocks the loop.
    """

    def __init__(self, port: int, debug: bool = False) -> None:
        self._port = port
        self._debug = debug
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[Optional[str]]] = None
        self._ready = threading.Event()

        try:
            import websockets  # type: ignore[import-untyped]  # noqa: F401
            self._has_websockets = True
        except ImportError:
            self._has_websockets = False
            logger.warning(
                "Install 'websockets' package for local mode: pip install recost[local]"
            )
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait briefly for the loop/queue to exist so the first send() after
        # init is guaranteed to land on the loop.
        self._ready.wait(timeout=1.0)

    def _run(self) -> None:
        """Owns the asyncio event loop for this transport."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue()
        self._ready.set()
        try:
            loop.run_until_complete(self._ws_loop())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _ws_loop(self) -> None:
        import websockets  # type: ignore[import-untyped]

        url = f"ws://127.0.0.1:{self._port}"
        assert self._queue is not None

        # Exponential backoff with ±25% jitter — same shape as the Node SDK's
        # Transport._computeBackoffMs(). The previous fixed-3s retry made
        # reconnect cost identical for a one-frame blip and a long extension
        # outage, which thrashed the local extension's accept loop.
        reconnect_attempts = 0

        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    reconnect_attempts = 0
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if msg is None:
                            return
                        try:
                            await ws.send(msg)
                        except Exception:
                            # Re-queue and reconnect. put_nowait is safe on an
                            # unbounded queue and cannot yield the loop.
                            self._queue.put_nowait(msg)
                            break
            except Exception:
                if not self._running:
                    return
                base = min(0.5 * (2 ** reconnect_attempts), 30.0)
                delay = base * (1 + (random.random() - 0.5) * 0.5)  # 0.75x..1.25x
                reconnect_attempts += 1
                await asyncio.sleep(delay)

    def send(self, payload: str) -> None:
        if not self._has_websockets or not self._running:
            return
        loop = self._loop
        queue_ = self._queue
        if loop is None or queue_ is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(queue_.put(payload), loop)
        except RuntimeError:
            # Loop was closed between the is_closed() check and the schedule.
            pass

    def dispose(self) -> None:
        self._running = False
        loop = self._loop
        queue_ = self._queue
        if self._has_websockets and loop is not None and queue_ is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(queue_.put(None), loop)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# Transport class
# ---------------------------------------------------------------------------


class Transport:
    """Delivers WindowSummary objects to the cloud API or the local VS Code extension."""

    def __init__(self, config: RecostConfig) -> None:
        self.mode: TransportMode = "cloud" if config.api_key else "local"
        self._api_key = config.api_key or ""
        self._project_id = config.project_id or ""
        self._base_url = config.base_url.rstrip("/")
        self._max_retries = config.max_retries
        self._max_buckets = config.max_buckets
        self._debug = config.debug
        self._on_error = config.on_error
        self._last_flush_status: Optional[FlushStatus] = None

        self._local: Optional[_LocalTransport] = None
        if self.mode == "local":
            self._local = _LocalTransport(config.local_port, config.debug)

    @property
    def last_flush_status(self) -> Optional[FlushStatus]:
        """Outcome of the most recent flush, or None if none has completed yet."""
        return self._last_flush_status

    def send(self, summary: WindowSummary) -> None:
        """Send a WindowSummary. Never raises — errors forwarded to on_error.

        If the summary has more than max_buckets metrics (degenerate burst
        case), it is split into chunks and sent sequentially. The
        ``last_flush_status`` property reflects the final chunk's outcome.
        """
        if len(summary.metrics) > self._max_buckets:
            chunk_size = self._max_buckets
            for i in range(0, len(summary.metrics), chunk_size):
                chunk = replace(summary, metrics=summary.metrics[i:i + chunk_size])
                self._send_one(chunk)
            return
        self._send_one(summary)

    def _send_one(self, summary: WindowSummary) -> None:
        body = json.dumps(summary.to_dict())
        window_size = len(summary.metrics)

        try:
            if self.mode == "cloud":
                url = f"{self._base_url}/projects/{self._project_id}/telemetry"
                ok, status, request_id = _post_cloud(
                    url, body, self._api_key, self._max_retries,
                )
                if not ok:
                    self._report_rejection(status, window_size, request_id)
                    self._last_flush_status = FlushStatus(
                        status="error", window_size=window_size, timestamp=_now_ms(),
                    )
                    return
                self._last_flush_status = FlushStatus(
                    status="ok", window_size=window_size, timestamp=_now_ms(),
                )
                return

            if self._local is not None:
                self._local.send(body)
            self._last_flush_status = FlushStatus(
                status="ok", window_size=window_size, timestamp=_now_ms(),
            )
        except Exception as exc:
            msg = f"[recost] transport error (windowSize={window_size}): {exc}"
            logger.warning(msg)
            if self._on_error is not None:
                self._on_error(exc)
            self._last_flush_status = FlushStatus(
                status="error", window_size=window_size, timestamp=_now_ms(),
            )

    def _report_rejection(
        self,
        status: int,
        window_size: int,
        request_id: Optional[str] = None,
    ) -> None:
        """Always warn on a non-2xx response (regardless of debug) and fire
        on_error. Silent rejection was how data used to be lost.

        We deliberately log only the status code, an internally-derived hint
        based on the status code, the window size, and (when present) the
        ``x-request-id`` response header — never the response body, which
        on 4xx can contain field-level validation detail that hints at
        project / key shape.
        """
        if status == 401:
            reason = "API key is invalid or has been revoked. Check RECOST_API_KEY."
        elif status == 403:
            reason = "API key does not have access to this project. Check RECOST_PROJECT_ID."
        elif status == 404:
            reason = "Project not found. Check RECOST_PROJECT_ID."
        elif status == 422:
            reason = "telemetry payload rejected (possibly over the 2000-bucket limit)"
        else:
            reason = "telemetry payload rejected"
        req_id_part = f" request_id={request_id}" if request_id else ""
        msg = (
            f"[recost] HTTP {status} — {reason} "
            f"(windowSize={window_size}{req_id_part})"
        )
        logger.warning(msg)
        if self._on_error is not None:
            self._on_error(Exception(msg))

    def dispose(self) -> None:
        """Clean up WebSocket thread / connections."""
        if self._local is not None:
            self._local.dispose()
            self._local = None


def _now_ms() -> int:
    return int(time.time() * 1000)
