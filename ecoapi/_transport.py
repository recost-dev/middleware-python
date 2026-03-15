"""
Transport — delivers WindowSummary payloads to either:
  - api.ecoapi.dev (cloud mode) via HTTPS POST with exponential-backoff retry, or
  - the EcoAPI VS Code extension (local mode) via WebSocket on localhost.

Uses urllib.request (stdlib) for cloud transport to avoid self-instrumentation
(the interceptor patches urllib3, not urllib.request).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from typing import Optional

from ._types import EcoAPIConfig, TransportMode, WindowSummary

logger = logging.getLogger("ecoapi")

# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------


def _post_cloud(
    url: str,
    body: str,
    api_key: str,
    max_retries: int,
) -> None:
    """POST a JSON body to the cloud API with retry."""
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "ecoapi-python/0.1.0",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=30)
            status = resp.getcode()
            if status and 200 <= status < 300:
                return
            # 4xx errors are not retriable
            if status and 400 <= status < 500:
                return
            last_error = Exception(f"HTTP {status}")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                    logger.error("[ecoapi] cloud rejected payload (%s): %s", e.code, body)
                except Exception:
                    pass
                return  # Don't retry 4xx
            last_error = e
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            time.sleep(min(1.0 * (2 ** attempt), 10.0))

    if last_error is not None:
        raise last_error


# ---------------------------------------------------------------------------
# Local WebSocket transport
# ---------------------------------------------------------------------------


class _LocalTransport:
    """Background-thread WebSocket transport for local mode."""

    def __init__(self, port: int, debug: bool = False) -> None:
        self._port = port
        self._debug = debug
        self._queue: queue.Queue[Optional[str]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._warned = False

        try:
            import websockets  # type: ignore[import-untyped]  # noqa: F401
            self._has_websockets = True
        except ImportError:
            self._has_websockets = False
            if not self._warned:
                logger.warning(
                    "Install 'websockets' package for local mode: pip install ecoapi[local]"
                )
                self._warned = True
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Background thread that manages the WebSocket connection."""
        import asyncio

        async def _ws_loop() -> None:
            import websockets  # type: ignore[import-untyped]

            url = f"ws://127.0.0.1:{self._port}"
            while self._running:
                try:
                    async with websockets.connect(url) as ws:
                        while self._running:
                            try:
                                msg = self._queue.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            if msg is None:
                                return
                            try:
                                await ws.send(msg)
                            except Exception:
                                # Re-queue and reconnect
                                self._queue.put(msg)
                                break
                except Exception:
                    if not self._running:
                        return
                    await asyncio.sleep(3.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_ws_loop())
        finally:
            loop.close()

    def send(self, payload: str) -> None:
        if not self._has_websockets:
            return
        self._queue.put(payload)

    def dispose(self) -> None:
        self._running = False
        if self._has_websockets:
            self._queue.put(None)  # Sentinel to unblock
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# Transport class
# ---------------------------------------------------------------------------


class Transport:
    """Delivers WindowSummary objects to the cloud API or the local VS Code extension."""

    def __init__(self, config: EcoAPIConfig) -> None:
        self.mode: TransportMode = "cloud" if config.api_key else "local"
        self._api_key = config.api_key or ""
        self._project_id = config.project_id or ""
        self._base_url = config.base_url.rstrip("/")
        self._max_retries = config.max_retries
        self._debug = config.debug
        self._on_error = config.on_error

        self._local: Optional[_LocalTransport] = None
        if self.mode == "local":
            self._local = _LocalTransport(config.local_port, config.debug)

    def send(self, summary: WindowSummary) -> None:
        """Send a WindowSummary. Never raises — errors forwarded to on_error."""
        body = json.dumps(summary.to_dict())

        try:
            if self.mode == "cloud":
                url = f"{self._base_url}/projects/{self._project_id}/telemetry"
                _post_cloud(url, body, self._api_key, self._max_retries)
            else:
                if self._local is not None:
                    self._local.send(body)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(exc)
            elif self._debug:
                logger.error("[ecoapi] transport error: %s", exc)

    def dispose(self) -> None:
        """Clean up WebSocket thread / connections."""
        if self._local is not None:
            self._local.dispose()
            self._local = None
