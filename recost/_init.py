"""
init() — wires the interceptor, provider registry, aggregator, and transport together.
This is the primary entry point for SDK users.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import warnings
from typing import Callable, List, Optional

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
        rebuild: Optional[Callable[["RecostHandle"], None]] = None,
    ) -> None:
        self._timer_stop = timer_stop
        self._timer_thread = timer_thread
        self._transport = transport
        self._final_flush = final_flush
        self._shutdown_flush_timeout_ms = shutdown_flush_timeout_ms
        self._disposed = False
        self.pid: int = os.getpid()
        self._rebuild = rebuild
        # Set by init() when auto_shutdown_handlers=True. Cleared in
        # dispose() so we don't keep a dangling atexit registration after
        # explicit teardown — without this, a long-lived process that
        # init/dispose's many times accumulates dead atexit callbacks.
        self._atexit_callback: Optional[Callable[[], None]] = None

    @property
    def last_flush_status(self) -> Optional[FlushStatus]:
        """Outcome of the most recent flush, or None if none has completed yet."""
        if self._transport is None:
            return None
        return self._transport.last_flush_status

    def reinit_after_fork(self) -> None:
        """Recreate timer thread + transport in the current PID. Idempotent within a PID."""
        if self._rebuild is None:
            return
        if (
            self.pid == os.getpid()
            and self._timer_thread is not None
            and self._timer_thread.is_alive()
        ):
            return  # No fork has happened; nothing to rebuild
        self._rebuild(self)
        self.pid = os.getpid()

    def dispose(self) -> None:
        """Stop intercepting, flush remaining events, and close transport connections.

        Stops the periodic timer first so no new flush can race the shutdown
        flush, then runs one final flush in a worker thread bounded by
        ``shutdown_flush_timeout_ms``. The transport is only disposed after
        the final flush settles or its timeout elapses, so an in-flight
        cloud POST is not cut off mid-request.

        Idempotent — safe to call from atexit and explicit teardown.
        """
        with _init_lock:
            if self._disposed:
                return
            self._disposed = True

            # Unregister the atexit hook so a long-lived process that
            # cycles init/dispose does not accumulate dead callbacks.
            # atexit.unregister is a no-op if the callable isn't currently
            # registered, so this is safe under any registration state.
            if self._atexit_callback is not None:
                try:
                    atexit.unregister(self._atexit_callback)
                except Exception:
                    pass
                self._atexit_callback = None

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


def _make_rebuild(
    config: RecostConfig,
    aggregator: Aggregator,
    flush_interval_seconds: float,
) -> Callable[[RecostHandle], None]:
    """Return a rebuild closure that recreates the transport and timer thread.

    Used by RecostHandle.reinit_after_fork() to restore the SDK in a forked
    child process. The closure intentionally creates its own simpler timer
    loop because the parent's _deferral cell does not survive fork.
    """

    def rebuild(handle: RecostHandle) -> None:
        # Replace transport (new event loop, new WS thread)
        new_transport = Transport(config)
        handle._transport = new_transport
        # Replace flush timer
        new_stop = threading.Event()
        handle._timer_stop = new_stop

        def _loop() -> None:
            while not new_stop.wait(timeout=flush_interval_seconds):
                try:
                    summary = aggregator.flush()
                    if summary is not None:
                        new_transport.send(summary)
                except Exception as err:
                    if config.on_error is not None:
                        config.on_error(err)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        handle._timer_thread = t

    return rebuild


# Module-level handle so a second init() call disposes the first.
_handle: Optional[RecostHandle] = None

# Guards init() and dispose() so the _handle global cannot become
# inconsistent under concurrent callers. RLock so a wrapping caller
# (e.g. dispose() running on the timer thread which itself owns the
# init lock) does not deadlock. See issue #4.
_init_lock: threading.RLock = threading.RLock()


def init(config: Optional[RecostConfig] = None) -> RecostHandle:
    """
    Initialize the Recost SDK.

    - Patches urllib3, httpx, and aiohttp.
    - Starts a flush interval that sends aggregated telemetry.
    - Returns a handle with a dispose() method for explicit cleanup.
    """
    global _handle
    with _init_lock:
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

        # PID backstop cells — populated after `handle` is constructed below.
        # The on_event closure reads these on every event so a forked child
        # whose register_at_fork hook didn't fire (uwsgi lazy-fork, etc.)
        # still self-repairs on the first intercepted call.
        _handle_ref: List[Optional["RecostHandle"]] = [None]
        _pid_warned: List[bool] = [False]

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
            # PID backstop: if the at-fork hook didn't fire on this platform,
            # repair the handle lazily on the first event after the fork.
            h = _handle_ref[0]
            if h is not None and h.pid != os.getpid():
                try:
                    h.reinit_after_fork()
                    if not _pid_warned[0]:
                        _pid_warned[0] = True
                        if config.on_error is not None:
                            from ._types import RecostError
                            config.on_error(RecostError(
                                f"recost: detected fork without register_at_fork hook; "
                                f"reinitialized in pid={os.getpid()}"
                            ))
                except Exception:
                    return  # never let SDK errors break user code

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

        rebuild = _make_rebuild(config, aggregator, flush_interval_seconds)
        handle = RecostHandle(
            timer_stop=stop_event,
            timer_thread=timer_thread,
            transport=transport,
            final_flush=_final_flush,
            shutdown_flush_timeout_ms=config.shutdown_flush_timeout_ms,
            rebuild=rebuild,
        )
        _handle = handle
        _handle_ref[0] = handle

        # Register an atexit hook so short-lived processes (cron, Lambda,
        # one-shot CLI scripts, SIGTERM'd containers) flush their last
        # bucket. The flush timer is a daemon thread and dies on exit;
        # without atexit the last window is silently dropped. The hook
        # delegates to handle.dispose(), which is idempotent — explicit
        # dispose() before exit is still fine.
        if config.auto_shutdown_handlers:
            handle._atexit_callback = handle.dispose
            atexit.register(handle._atexit_callback)

        if hasattr(os, "register_at_fork"):
            def _after_fork() -> None:
                try:
                    handle.reinit_after_fork()
                except Exception:
                    pass  # Never let SDK errors crash a forked child
            os.register_at_fork(after_in_child=_after_fork)

        return handle
