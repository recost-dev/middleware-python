"""
Flask extension adapter for recost.

Usage:
    from flask import Flask
    from recost.frameworks.flask import RecostExtension

    app = Flask(__name__)
    ext = RecostExtension(app, api_key="...", project_id="...")
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

from .._init import init, RecostHandle
from .._types import RecostConfig

try:
    from flask import Flask

    class RecostExtension:
        """Flask extension that initializes Recost telemetry."""

        def __init__(
            self,
            app: Optional[Flask] = None,
            config: Optional[RecostConfig] = None,
            **kwargs: Any,
        ) -> None:
            self._handle: Optional[RecostHandle] = None
            if app is not None:
                self.init_app(app, config, **kwargs)

        def init_app(
            self,
            app: Flask,
            config: Optional[RecostConfig] = None,
            **kwargs: Any,
        ) -> None:
            if config is None:
                config = RecostConfig(**kwargs)
            self._handle = init(config)

    class ReCost(RecostExtension):
        """Deprecated alias for :class:`RecostExtension`.

        Will be removed in a future release. Switch to
        ``from recost.frameworks.flask import RecostExtension``.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            warnings.warn(
                "recost.frameworks.flask.ReCost is deprecated and will be "
                "removed in a future release; use RecostExtension instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            super().__init__(*args, **kwargs)

except ImportError:
    class RecostExtension:  # type: ignore[no-redef]
        """Stub — install 'flask' to use: pip install recost[flask]"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "flask is required for the Recost extension. "
                "Install it with: pip install recost[flask]"
            )

    class ReCost(RecostExtension):  # type: ignore[no-redef]
        """Stub deprecated alias — install 'flask' to use."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            warnings.warn(
                "recost.frameworks.flask.ReCost is deprecated; use "
                "RecostExtension instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            super().__init__(*args, **kwargs)
