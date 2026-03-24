"""
FastAPI / Starlette ASGI middleware adapter for recost.

Usage:
    from fastapi import FastAPI
    from recost.frameworks.fastapi import RecostMiddleware

    app = FastAPI()
    app.add_middleware(RecostMiddleware, api_key="...", project_id="...")
"""

from __future__ import annotations

from typing import Any, Optional

from .._init import init
from .._types import RecostConfig

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class RecostMiddleware(BaseHTTPMiddleware):
        """ASGI middleware that initializes ReCost telemetry."""

        def __init__(self, app: Any, config: Optional[RecostConfig] = None, **kwargs: Any) -> None:
            super().__init__(app)
            if config is None:
                config = RecostConfig(**kwargs)
            self._handle = init(config)

        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)

except ImportError:
    # starlette not installed — provide a stub that raises on use
    class RecostMiddleware:  # type: ignore[no-redef]
        """Stub — install 'starlette' to use: pip install recost[fastapi]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "starlette is required for RecostMiddleware. "
                "Install it with: pip install recost[fastapi]"
            )
