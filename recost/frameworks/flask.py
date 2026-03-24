"""
Flask extension adapter for recost.

Usage:
    from flask import Flask
    from recost.frameworks.flask import ReCost

    app = Flask(__name__)
    eco = ReCost(app, api_key="...", project_id="...")
"""

from __future__ import annotations

from typing import Any, Optional

from .._init import init
from .._types import RecostConfig

try:
    from flask import Flask

    class ReCost:
        """Flask extension that initializes ReCost telemetry."""

        def __init__(self, app: Optional[Flask] = None, config: Optional[RecostConfig] = None, **kwargs: Any) -> None:
            self._handle = None
            if app is not None:
                self.init_app(app, config, **kwargs)

        def init_app(self, app: Flask, config: Optional[RecostConfig] = None, **kwargs: Any) -> None:
            if config is None:
                config = RecostConfig(**kwargs)
            self._handle = init(config)

except ImportError:
    class ReCost:  # type: ignore[no-redef]
        """Stub — install 'flask' to use: pip install recost[flask]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "flask is required for ReCost extension. "
                "Install it with: pip install recost[flask]"
            )
