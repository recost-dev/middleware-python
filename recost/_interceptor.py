"""
Interceptor — monkey-patches urllib3, httpx, and aiohttp to capture outbound
request metadata as RawEvents.

Singleton module. Only one set of patches can be active at a time.
The interceptor never reads or modifies request/response bodies beyond size.
Every wrapper is safety-wrapped so SDK errors can never break application code.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextvars import Token
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from ._types import RawEvent

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

EventCallback = Callable[[RawEvent], None]

# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

_installed: bool = False
_callback: Optional[EventCallback] = None

# Guards install / uninstall / is_installed so the patched-method state
# cannot drift away from the _installed flag under concurrent callers.
# RLock so the same thread can re-enter (e.g. from a callback that
# triggers reinstall). See issue #4.
_install_lock: threading.RLock = threading.RLock()

# Original function references — restored on uninstall
_original_urllib3_urlopen = None
_original_httpx_send = None
_original_httpx_async_send = None
_original_aiohttp_request = None

# Sentinel for urllib3 timeout default — resolved lazily to avoid import-time side-effects
_UNSET: Any = object()

# Double-count prevention — covers both async-task hops and OS-thread hops.
_in_interceptor_task: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_in_interceptor_task", default=False
)
_in_interceptor_thread = threading.local()


def _is_in_interceptor() -> bool:
    return _in_interceptor_task.get() or bool(getattr(_in_interceptor_thread, "flag", False))


def _enter_interceptor() -> Token[bool]:
    token = _in_interceptor_task.set(True)
    _in_interceptor_thread.flag = True
    return token


def _exit_interceptor(token: Token[bool]) -> None:
    _in_interceptor_task.reset(token)
    _in_interceptor_thread.flag = False

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _strip_query(url: str) -> str:
    """Strip query string and fragment from URL, preserving scheme/netloc/path.

    Uses urlparse so encoded characters in the path (%3F, %23, etc.) and
    URL fragments cannot leak into event.url. The substring-based
    predecessor was correct for canonical URLs but fragile for any input
    where a `?` or `#` could appear in a non-query position, and it did
    not drop userinfo from the netloc.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            return url
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        path = parsed.path or ""
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url


def _build_event(
    url: str,
    method: str,
    status_code: int,
    latency_ms: float,
    request_bytes: int,
    response_bytes: int,
) -> RawEvent:
    """Build a RawEvent from captured request metadata."""
    clean_url = _strip_query(url)
    try:
        parsed = urlparse(clean_url)
        host = parsed.hostname or ""
        path = parsed.path or "/"
    except Exception:
        host = ""
        path = "/"

    return RawEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        method=method.upper(),
        url=clean_url,
        host=host,
        path=path,
        status_code=status_code,
        latency_ms=latency_ms,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        provider=None,
        endpoint_category=None,
        error=status_code == 0 or status_code >= 400,
    )


# ---------------------------------------------------------------------------
# urllib3 patch
# ---------------------------------------------------------------------------


def _patch_urllib3() -> None:
    global _original_urllib3_urlopen
    try:
        import urllib3
    except ImportError:
        return

    _original_urllib3_urlopen = urllib3.HTTPConnectionPool.urlopen

    def _patched_urlopen(self: Any, method: str, url: str, body: Any = None, headers: Any = None, retries: Any = None, redirect: bool = True, assert_same_host: bool = True, timeout: Any = _UNSET, **response_kw: Any) -> Any:
        if timeout is _UNSET:
            timeout = urllib3.util.Timeout.DEFAULT_TIMEOUT
        if _is_in_interceptor():
            return _original_urllib3_urlopen(self, method, url, body=body, headers=headers, retries=retries, redirect=redirect, assert_same_host=assert_same_host, timeout=timeout, **response_kw)

        token = _enter_interceptor()
        start = time.perf_counter()
        full_url = ""
        request_bytes = 0

        try:
            # Reconstruct full URL
            scheme = self.scheme if hasattr(self, "scheme") else "http"
            host = self.host if hasattr(self, "host") else "localhost"
            port = self.port if hasattr(self, "port") else None
            port_str = f":{port}" if port and port not in (80, 443) else ""
            full_url = f"{scheme}://{host}{port_str}{url}"

            if body is not None:
                if isinstance(body, bytes):
                    request_bytes = len(body)
                elif isinstance(body, str):
                    request_bytes = len(body.encode("utf-8", errors="replace"))
        except Exception:
            pass

        try:
            response = _original_urllib3_urlopen(self, method, url, body=body, headers=headers, retries=retries, redirect=redirect, assert_same_host=assert_same_host, timeout=timeout, **response_kw)
            _exit_interceptor(token)

            try:
                latency_ms = (time.perf_counter() - start) * 1000
                status_code = response.status if hasattr(response, "status") else 0
                cl = response.headers.get("content-length", "0") if hasattr(response, "headers") and response.headers else "0"
                response_bytes = int(cl) if cl and cl.isdigit() else 0
                if _callback is not None:
                    _callback(_build_event(full_url, method, status_code, latency_ms, request_bytes, response_bytes))
            except Exception:
                pass

            return response
        except Exception:
            _exit_interceptor(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise

    urllib3.HTTPConnectionPool.urlopen = _patched_urlopen  # type: ignore[method-assign,assignment]


def _unpatch_urllib3() -> None:
    global _original_urllib3_urlopen
    if _original_urllib3_urlopen is None:
        return
    try:
        import urllib3
        urllib3.HTTPConnectionPool.urlopen = _original_urllib3_urlopen  # type: ignore[method-assign]
    except ImportError:
        pass
    _original_urllib3_urlopen = None


# ---------------------------------------------------------------------------
# httpx patch
# ---------------------------------------------------------------------------


def _patch_httpx() -> None:
    global _original_httpx_send, _original_httpx_async_send
    try:
        import httpx
    except ImportError:
        return

    # Sync client
    _original_httpx_send = httpx.Client.send

    def _patched_send(self: Any, request: Any, **kwargs: Any) -> Any:
        if _is_in_interceptor():
            return _original_httpx_send(self, request, **kwargs)

        token = _enter_interceptor()
        start = time.perf_counter()
        full_url = str(request.url) if hasattr(request, "url") else ""
        request_bytes = 0

        try:
            content = getattr(request, "content", None)
            if isinstance(content, (bytes, bytearray)):
                request_bytes = len(content)
            # Non-bytes (streaming/async-iterable) — skip sizing; never materialize.
        except Exception:
            pass

        try:
            response = _original_httpx_send(self, request, **kwargs)
            _exit_interceptor(token)

            try:
                latency_ms = (time.perf_counter() - start) * 1000
                status_code = response.status_code
                cl = response.headers.get("content-length", "0")
                response_bytes = int(cl) if cl and cl.isdigit() else 0
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, status_code, latency_ms, request_bytes, response_bytes))
            except Exception:
                pass

            return response
        except Exception:
            _exit_interceptor(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise

    httpx.Client.send = _patched_send  # type: ignore[method-assign]

    # Async client
    _original_httpx_async_send = httpx.AsyncClient.send

    async def _patched_async_send(self: Any, request: Any, **kwargs: Any) -> Any:
        if _is_in_interceptor():
            return await _original_httpx_async_send(self, request, **kwargs)

        token = _enter_interceptor()
        start = time.perf_counter()
        full_url = str(request.url) if hasattr(request, "url") else ""
        request_bytes = 0

        try:
            content = getattr(request, "content", None)
            if isinstance(content, (bytes, bytearray)):
                request_bytes = len(content)
            # Non-bytes (streaming/async-iterable) — skip sizing; never materialize.
        except Exception:
            pass

        try:
            response = await _original_httpx_async_send(self, request, **kwargs)
            _exit_interceptor(token)

            try:
                latency_ms = (time.perf_counter() - start) * 1000
                status_code = response.status_code
                cl = response.headers.get("content-length", "0")
                response_bytes = int(cl) if cl and cl.isdigit() else 0
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, status_code, latency_ms, request_bytes, response_bytes))
            except Exception:
                pass

            return response
        except Exception:
            _exit_interceptor(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise

    httpx.AsyncClient.send = _patched_async_send  # type: ignore[method-assign]


def _unpatch_httpx() -> None:
    global _original_httpx_send, _original_httpx_async_send
    try:
        import httpx
    except ImportError:
        return
    if _original_httpx_send is not None:
        httpx.Client.send = _original_httpx_send  # type: ignore[method-assign]
        _original_httpx_send = None
    if _original_httpx_async_send is not None:
        httpx.AsyncClient.send = _original_httpx_async_send  # type: ignore[method-assign]
        _original_httpx_async_send = None


# ---------------------------------------------------------------------------
# aiohttp patch
# ---------------------------------------------------------------------------


def _patch_aiohttp() -> None:
    global _original_aiohttp_request
    try:
        import aiohttp
    except ImportError:
        return

    _original_aiohttp_request = aiohttp.ClientSession._request

    async def _patched_request(self: Any, method: str, url: Any, **kwargs: Any) -> Any:
        if _is_in_interceptor():
            return await _original_aiohttp_request(self, method, url, **kwargs)

        token = _enter_interceptor()
        start = time.perf_counter()
        full_url = str(url)
        request_bytes = 0

        try:
            data = kwargs.get("data")
            json_body = kwargs.get("json")
            if json_body is not None:
                try:
                    import json as _json
                    request_bytes = len(_json.dumps(json_body))
                except (TypeError, ValueError):
                    request_bytes = 0
            elif data is not None:
                if isinstance(data, (bytes, bytearray)):
                    request_bytes = len(data)
                elif isinstance(data, str):
                    request_bytes = len(data.encode("utf-8", errors="replace"))
                elif hasattr(data, "_size"):
                    size_attr = getattr(data, "_size", None)
                    if isinstance(size_attr, int) and size_attr >= 0:
                        request_bytes = size_attr
                elif callable(data):
                    # FormData and similar callables produce a payload when invoked
                    try:
                        payload = data()
                        size_attr = getattr(payload, "_size", None)
                        if isinstance(size_attr, int) and size_attr >= 0:
                            request_bytes = size_attr
                    except Exception:
                        pass
                # async iterables, BytesIO without known size — leave at 0
        except Exception:
            pass

        try:
            response = await _original_aiohttp_request(self, method, url, **kwargs)
            _exit_interceptor(token)

            try:
                latency_ms = (time.perf_counter() - start) * 1000
                status_code = response.status if hasattr(response, "status") else 0
                cl = response.headers.get("content-length", "0") if hasattr(response, "headers") else "0"
                response_bytes = int(cl) if cl and cl.isdigit() else 0
                if _callback is not None:
                    _callback(_build_event(full_url, method, status_code, latency_ms, request_bytes, response_bytes))
            except Exception:
                pass

            return response
        except Exception:
            _exit_interceptor(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise

    aiohttp.ClientSession._request = _patched_request  # type: ignore[method-assign,assignment]


def _unpatch_aiohttp() -> None:
    global _original_aiohttp_request
    if _original_aiohttp_request is None:
        return
    try:
        import aiohttp
        aiohttp.ClientSession._request = _original_aiohttp_request  # type: ignore[method-assign]
    except ImportError:
        pass
    _original_aiohttp_request = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(callback: EventCallback) -> None:
    """Install patches on urllib3, httpx, and aiohttp. No-op if already installed."""
    global _installed, _callback
    with _install_lock:
        if _installed:
            return

        _callback = callback
        _patch_urllib3()
        _patch_httpx()
        _patch_aiohttp()
        _installed = True


def uninstall() -> None:
    """Restore all patched functions to their originals. No-op if not installed."""
    global _installed, _callback
    with _install_lock:
        if not _installed:
            return

        _unpatch_urllib3()
        _unpatch_httpx()
        _unpatch_aiohttp()
        _callback = None
        _installed = False


def is_installed() -> bool:
    """Returns True if patches are currently active."""
    with _install_lock:
        return _installed
