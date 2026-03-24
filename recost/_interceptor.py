"""
Interceptor — monkey-patches urllib3, httpx, and aiohttp to capture outbound
request metadata as RawEvents.

Singleton module. Only one set of patches can be active at a time.
The interceptor never reads or modifies request/response bodies beyond size.
Every wrapper is safety-wrapped so SDK errors can never break application code.
"""

from __future__ import annotations

import contextvars
import time
from datetime import datetime, timezone
from typing import Callable, Optional
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

# Original function references — restored on uninstall
_original_urllib3_urlopen = None
_original_httpx_send = None
_original_httpx_async_send = None
_original_aiohttp_request = None

# Double-count prevention
_in_interceptor: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_in_interceptor", default=False
)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _strip_query(url: str) -> str:
    """Strip query string from URL."""
    try:
        idx = url.find("?")
        if idx != -1:
            return url[:idx]
        return url
    except Exception:
        return url


def _build_event(
    url: str,
    method: str,
    status_code: int,
    latency_ms: int,
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
        latency_ms=round(latency_ms),
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
        import urllib3  # type: ignore[import-untyped]
    except ImportError:
        return

    _original_urllib3_urlopen = urllib3.HTTPConnectionPool.urlopen  # type: ignore[attr-defined]

    def _patched_urlopen(self, method, url, body=None, headers=None, retries=None, redirect=True, assert_same_host=True, timeout=urllib3.util.Timeout.DEFAULT_TIMEOUT, pool_connections=None, pool_maxsize=None, **response_kw):  # type: ignore[no-untyped-def]
        if _in_interceptor.get(False):
            return _original_urllib3_urlopen(self, method, url, body=body, headers=headers, retries=retries, redirect=redirect, assert_same_host=assert_same_host, timeout=timeout, **response_kw)

        token = _in_interceptor.set(True)
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
            _in_interceptor.reset(token)

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
        except Exception as exc:
            _in_interceptor.reset(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise exc

    urllib3.HTTPConnectionPool.urlopen = _patched_urlopen  # type: ignore[attr-defined]


def _unpatch_urllib3() -> None:
    global _original_urllib3_urlopen
    if _original_urllib3_urlopen is None:
        return
    try:
        import urllib3  # type: ignore[import-untyped]
        urllib3.HTTPConnectionPool.urlopen = _original_urllib3_urlopen  # type: ignore[attr-defined]
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

    def _patched_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if _in_interceptor.get(False):
            return _original_httpx_send(self, request, **kwargs)

        token = _in_interceptor.set(True)
        start = time.perf_counter()
        full_url = str(request.url) if hasattr(request, "url") else ""
        request_bytes = 0

        try:
            if hasattr(request, "content") and request.content is not None:
                request_bytes = len(request.content)
        except Exception:
            pass

        try:
            response = _original_httpx_send(self, request, **kwargs)
            _in_interceptor.reset(token)

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
        except Exception as exc:
            _in_interceptor.reset(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise exc

    httpx.Client.send = _patched_send  # type: ignore[assignment]

    # Async client
    _original_httpx_async_send = httpx.AsyncClient.send

    async def _patched_async_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if _in_interceptor.get(False):
            return await _original_httpx_async_send(self, request, **kwargs)

        token = _in_interceptor.set(True)
        start = time.perf_counter()
        full_url = str(request.url) if hasattr(request, "url") else ""
        request_bytes = 0

        try:
            if hasattr(request, "content") and request.content is not None:
                request_bytes = len(request.content)
        except Exception:
            pass

        try:
            response = await _original_httpx_async_send(self, request, **kwargs)
            _in_interceptor.reset(token)

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
        except Exception as exc:
            _in_interceptor.reset(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                method = request.method if hasattr(request, "method") else "GET"
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise exc

    httpx.AsyncClient.send = _patched_async_send  # type: ignore[assignment]


def _unpatch_httpx() -> None:
    global _original_httpx_send, _original_httpx_async_send
    try:
        import httpx
    except ImportError:
        return
    if _original_httpx_send is not None:
        httpx.Client.send = _original_httpx_send  # type: ignore[assignment]
        _original_httpx_send = None
    if _original_httpx_async_send is not None:
        httpx.AsyncClient.send = _original_httpx_async_send  # type: ignore[assignment]
        _original_httpx_async_send = None


# ---------------------------------------------------------------------------
# aiohttp patch
# ---------------------------------------------------------------------------


def _patch_aiohttp() -> None:
    global _original_aiohttp_request
    try:
        import aiohttp  # type: ignore[import-untyped]
    except ImportError:
        return

    _original_aiohttp_request = aiohttp.ClientSession._request  # type: ignore[attr-defined]

    async def _patched_request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        if _in_interceptor.get(False):
            return await _original_aiohttp_request(self, method, url, **kwargs)

        token = _in_interceptor.set(True)
        start = time.perf_counter()
        full_url = str(url)
        request_bytes = 0

        try:
            data = kwargs.get("data")
            if data is not None:
                if isinstance(data, bytes):
                    request_bytes = len(data)
                elif isinstance(data, str):
                    request_bytes = len(data.encode("utf-8", errors="replace"))
        except Exception:
            pass

        try:
            response = await _original_aiohttp_request(self, method, url, **kwargs)
            _in_interceptor.reset(token)

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
        except Exception as exc:
            _in_interceptor.reset(token)
            try:
                latency_ms = (time.perf_counter() - start) * 1000
                if _callback is not None:
                    _callback(_build_event(full_url, method, 0, latency_ms, request_bytes, 0))
            except Exception:
                pass
            raise exc

    aiohttp.ClientSession._request = _patched_request  # type: ignore[attr-defined]


def _unpatch_aiohttp() -> None:
    global _original_aiohttp_request
    if _original_aiohttp_request is None:
        return
    try:
        import aiohttp  # type: ignore[import-untyped]
        aiohttp.ClientSession._request = _original_aiohttp_request  # type: ignore[attr-defined]
    except ImportError:
        pass
    _original_aiohttp_request = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(callback: EventCallback) -> None:
    """Install patches on urllib3, httpx, and aiohttp. No-op if already installed."""
    global _installed, _callback
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
    if not _installed:
        return

    _unpatch_urllib3()
    _unpatch_httpx()
    _unpatch_aiohttp()
    _callback = None
    _installed = False


def is_installed() -> bool:
    """Returns True if patches are currently active."""
    return _installed
