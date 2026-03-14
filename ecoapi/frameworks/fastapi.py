"""
FastAPI / Starlette ASGI middleware adapter for ecoapi.

Usage:
    from fastapi import FastAPI
    from ecoapi.frameworks.fastapi import EcoAPIMiddleware

    app = FastAPI()
    app.add_middleware(EcoAPIMiddleware, api_key="...", project_id="...")
"""

from __future__ import annotations

from typing import Any, Optional

from .._init import init
from .._types import EcoAPIConfig

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class EcoAPIMiddleware(BaseHTTPMiddleware):
        """ASGI middleware that initializes EcoAPI telemetry."""

        def __init__(self, app: Any, config: Optional[EcoAPIConfig] = None, **kwargs: Any) -> None:
            super().__init__(app)
            if config is None:
                config = EcoAPIConfig(**kwargs)
            self._handle = init(config)

        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)

except ImportError:
    # starlette not installed — provide a stub that raises on use
    class EcoAPIMiddleware:  # type: ignore[no-redef]
        """Stub — install 'starlette' to use: pip install ecoapi[fastapi]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "starlette is required for EcoAPIMiddleware. "
                "Install it with: pip install ecoapi[fastapi]"
            )
