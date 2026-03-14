"""
init() — wires the interceptor, provider registry, aggregator, and transport together.
This is the primary entry point for SDK users.
"""

from __future__ import annotations

import sys
import threading
from typing import Optional

from ._aggregator import Aggregator
from ._interceptor import install, uninstall
from ._provider_registry import ProviderRegistry
from ._transport import Transport
from ._types import EcoAPIConfig, RawEvent


class EcoAPIHandle:
    """Returned by init() to allow explicit teardown."""

    def __init__(
        self,
        timer_stop: threading.Event,
        timer_thread: Optional[threading.Thread],
        transport: Optional[Transport],
    ) -> None:
        self._timer_stop = timer_stop
        self._timer_thread = timer_thread
        self._transport = transport
        self._disposed = False

    def dispose(self) -> None:
        """Stop intercepting, flush remaining events, and close transport connections."""
        if self._disposed:
            return
        self._disposed = True

        self._timer_stop.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=5.0)

        uninstall()

        if self._transport is not None:
            self._transport.dispose()

        global _handle
        if _handle is self:
            _handle = None


# Module-level handle so a second init() call disposes the first.
_handle: Optional[EcoAPIHandle] = None


def init(config: Optional[EcoAPIConfig] = None) -> EcoAPIHandle:
    """
    Initialize the EcoAPI SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    if _handle is not None:
        _handle.dispose()

    config = config or EcoAPIConfig()

    if not config.enabled:
        stop_event = threading.Event()
        stop_event.set()
        noop = EcoAPIHandle(timer_stop=stop_event, timer_thread=None, transport=None)
        _handle = noop
        return noop

    registry = ProviderRegistry(config.custom_providers or None)
    aggregator = Aggregator(
        project_id=config.project_id or "",
        environment=config.environment,
        sdk_version="0.1.0",
    )
    transport = Transport(config)
    debug = config.debug
    max_batch_size = config.max_batch_size

    # Build the set of URL substrings to exclude from tracking.
    exclude_patterns = list(config.exclude_patterns)
    if config.api_key:
        exclude_patterns.append(config.base_url.rstrip("/"))
    else:
        exclude_patterns.append(f"127.0.0.1:{config.local_port}")
        exclude_patterns.append(f"localhost:{config.local_port}")

    def flush_and_send() -> None:
        summary = aggregator.flush()
        if summary is None:
            return
        if debug:
            print(
                f"[ecoapi] flush: {len(summary.metrics)} metric group(s), "
                f"window {summary.window_start} → {summary.window_end}",
                file=sys.stderr,
            )
        transport.send(summary)

    def on_event(event: RawEvent) -> None:
        # Drop excluded URLs
        for pattern in exclude_patterns:
            if pattern in event.url or pattern in event.host:
                return

        # Enrich with provider/endpoint from the registry
        match = registry.match(event.url)
        if match is not None:
            event.provider = match.provider
            event.endpoint_category = match.endpoint_category

        if debug:
            print(
                f"[ecoapi] captured {event.method} {event.url} "
                f"{event.status_code} ({event.latency_ms}ms)",
                file=sys.stderr,
            )

        cost = match.cost_per_request_cents if match is not None else 0.0
        aggregator.ingest(event, cost)

        # Trigger an early flush if the batch size threshold is reached
        if aggregator.size >= max_batch_size:
            try:
                flush_and_send()
            except Exception as err:
                if config.on_error:
                    config.on_error(err)
                elif debug:
                    print(f"[ecoapi] flush error: {err}", file=sys.stderr)

    install(on_event)

    # Flush timer using a background thread with Event-based stopping
    stop_event = threading.Event()

    def _timer_loop() -> None:
        while not stop_event.wait(timeout=config.flush_interval):
            try:
                flush_and_send()
            except Exception as err:
                if config.on_error:
                    config.on_error(err)
                elif debug:
                    print(f"[ecoapi] flush error: {err}", file=sys.stderr)

    timer_thread = threading.Thread(target=_timer_loop, daemon=True)
    timer_thread.start()

    handle = EcoAPIHandle(
        timer_stop=stop_event,
        timer_thread=timer_thread,
        transport=transport,
    )
    _handle = handle
    return handle
