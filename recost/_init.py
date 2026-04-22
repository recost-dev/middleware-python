"""
init() — wires the interceptor, provider registry, aggregator, and transport together.
This is the primary entry point for SDK users.
"""

from __future__ import annotations

import sys
import threading
import warnings
from typing import Callable, Optional

from ._aggregator import Aggregator
from ._interceptor import install, uninstall
from ._provider_registry import ProviderRegistry
from ._transport import Transport
from ._types import FlushStatus, RecostConfig, RawEvent


class RecostHandle:
    """Returned by init() to allow explicit teardown."""

    def __init__(
        self,
        timer_stop: threading.Event,
        timer_thread: Optional[threading.Thread],
        transport: Optional[Transport],
        final_flush: Optional[Callable[[], None]] = None,
        shutdown_flush_timeout_ms: int = 3_000,
    ) -> None:
        self._timer_stop = timer_stop
        self._timer_thread = timer_thread
        self._transport = transport
        self._final_flush = final_flush
        self._shutdown_flush_timeout_ms = shutdown_flush_timeout_ms
        self._disposed = False

    @property
    def last_flush_status(self) -> Optional[FlushStatus]:
        """Outcome of the most recent flush, or None if none has completed yet."""
        if self._transport is None:
            return None
        return self._transport.last_flush_status

    def dispose(self) -> None:
        """Stop intercepting, flush remaining events, and close transport connections.

        Stops the periodic timer first so no new flush can race the shutdown
        flush, then runs one final flush in a worker thread bounded by
        ``shutdown_flush_timeout_ms``. The transport is only disposed after
        the final flush settles or its timeout elapses, so an in-flight
        cloud POST is not cut off mid-request.
        """
        if self._disposed:
            return
        self._disposed = True

        self._timer_stop.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=5.0)

        if self._final_flush is not None:
            flush_thread = threading.Thread(target=self._final_flush, daemon=True)
            flush_thread.start()
            flush_thread.join(timeout=self._shutdown_flush_timeout_ms / 1000.0)

        uninstall()

        if self._transport is not None:
            self._transport.dispose()

        global _handle
        if _handle is self:
            _handle = None


# Module-level handle so a second init() call disposes the first.
_handle: Optional[RecostHandle] = None


def init(config: Optional[RecostConfig] = None) -> RecostHandle:
    """
    Initialize the ReCost SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    if _handle is not None:
        _handle.dispose()

    config = config or RecostConfig()

    # Resolve flush interval: prefer the new ms-based field, but if a caller
    # still passes the legacy seconds-based flush_interval, honor it with a
    # deprecation warning so existing code keeps working until they migrate.
    if config.flush_interval is not None:
        warnings.warn(
            "flush_interval is deprecated, use flush_interval_ms instead",
            DeprecationWarning,
            stacklevel=2,
        )
        flush_interval_seconds = float(config.flush_interval)
    else:
        flush_interval_seconds = config.flush_interval_ms / 1000.0

    if not config.enabled:
        stop_event = threading.Event()
        stop_event.set()
        noop = RecostHandle(timer_stop=stop_event, timer_thread=None, transport=None)
        _handle = noop
        return noop

    registry = ProviderRegistry(config.custom_providers or None)
    aggregator = Aggregator(
        project_id=config.project_id or "",
        environment=config.environment,
        sdk_version="0.1.0",
        max_buckets=config.max_buckets,
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
                f"[recost] flush: {len(summary.metrics)} metric group(s), "
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
                f"[recost] captured {event.method} {event.url} "
                f"{event.status_code} ({event.latency_ms}ms)",
                file=sys.stderr,
            )

        # If this event would push us past the bucket cap, flush the current
        # window first so it's preserved, then ingest into a fresh window.
        if aggregator.would_overflow(event):
            try:
                flush_and_send()
            except Exception as err:
                if config.on_error:
                    config.on_error(err)
                elif debug:
                    print(f"[recost] flush error: {err}", file=sys.stderr)

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
                    print(f"[recost] flush error: {err}", file=sys.stderr)

    install(on_event)

    # Flush timer using a background thread with Event-based stopping
    stop_event = threading.Event()

    def _timer_loop() -> None:
        while not stop_event.wait(timeout=flush_interval_seconds):
            try:
                flush_and_send()
            except Exception as err:
                if config.on_error:
                    config.on_error(err)
                elif debug:
                    print(f"[recost] flush error: {err}", file=sys.stderr)

    timer_thread = threading.Thread(target=_timer_loop, daemon=True)
    timer_thread.start()

    def _final_flush() -> None:
        # Errors during the final flush are logged / forwarded the same way
        # as a normal tick — we never want dispose() to surface them.
        try:
            flush_and_send()
        except Exception as err:
            if config.on_error:
                config.on_error(err)
            elif debug:
                print(f"[recost] flush error: {err}", file=sys.stderr)

    handle = RecostHandle(
        timer_stop=stop_event,
        timer_thread=timer_thread,
        transport=transport,
        final_flush=_final_flush,
        shutdown_flush_timeout_ms=config.shutdown_flush_timeout_ms,
    )
    _handle = handle
    return handle
