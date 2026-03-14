"""
Flask extension adapter for ecoapi.

Usage:
    from flask import Flask
    from ecoapi.frameworks.flask import EcoAPI

    app = Flask(__name__)
    eco = EcoAPI(app, api_key="...", project_id="...")
"""

from __future__ import annotations

from typing import Any, Optional

from .._init import init
from .._types import EcoAPIConfig

try:
    from flask import Flask

    class EcoAPI:
        """Flask extension that initializes EcoAPI telemetry."""

        def __init__(self, app: Optional[Flask] = None, config: Optional[EcoAPIConfig] = None, **kwargs: Any) -> None:
            self._handle = None
            if app is not None:
                self.init_app(app, config, **kwargs)

        def init_app(self, app: Flask, config: Optional[EcoAPIConfig] = None, **kwargs: Any) -> None:
            if config is None:
                config = EcoAPIConfig(**kwargs)
            self._handle = init(config)

except ImportError:
    class EcoAPI:  # type: ignore[no-redef]
        """Stub — install 'flask' to use: pip install ecoapi[flask]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "flask is required for EcoAPI extension. "
                "Install it with: pip install ecoapi[flask]"
            )
