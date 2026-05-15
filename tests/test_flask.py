"""
Tests for recost/frameworks/flask.py
"""

import warnings

from recost._interceptor import is_installed, uninstall
from recost._types import RecostConfig


class TestRecostExtension:
    def test_extension_initializes_interceptor(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            app = Flask(__name__)
            RecostExtension(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_init_app_pattern(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            ext = RecostExtension()
            app = Flask(__name__)
            ext.init_app(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_accepts_kwargs(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import RecostExtension

            app = Flask(__name__)
            RecostExtension(app, enabled=True, debug=False)
            assert is_installed()
        finally:
            uninstall()


class TestReCostDeprecationAlias:
    """The old `ReCost` name must keep working for one release but emit a
    DeprecationWarning so users migrate to `RecostExtension`."""

    def test_old_name_still_constructs(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import ReCost

            app = Flask(__name__)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ReCost(app, config=RecostConfig(enabled=True))
            assert is_installed()
            deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert deprecations, "expected a DeprecationWarning on ReCost(...)"
            assert "RecostExtension" in str(deprecations[0].message)
        finally:
            uninstall()

    def test_old_name_is_subclass_or_alias_of_new(self):
        """Importing the old name must yield the same class as the new name
        so isinstance checks and existing type annotations keep working."""
        from recost.frameworks.flask import ReCost, RecostExtension

        assert ReCost is RecostExtension or issubclass(ReCost, RecostExtension)


def test_flask_extension_raises_clear_error_without_flask(monkeypatch) -> None:
    """When `flask` is not installed, importing
    `recost.frameworks.flask.RecostExtension` yields the stub class whose
    constructor raises ImportError with an install hint.

    We fake the missing import via sys.modules + importlib.reload, then
    reload again with the real module after the test so subsequent tests
    aren't poisoned.
    """
    import importlib
    import sys
    import pytest as _pytest

    monkeypatch.setitem(sys.modules, "flask", None)
    import recost.frameworks.flask as flask_mod
    importlib.reload(flask_mod)
    try:
        with _pytest.raises(ImportError, match="pip install recost\\[flask\\]"):
            flask_mod.RecostExtension()
    finally:
        monkeypatch.undo()
        importlib.reload(flask_mod)


class TestInitAppDisposePrevious:
    """init_app() must dispose self._handle before reassigning so the
    extension instance never carries a stale handle reference."""

    def test_init_app_disposes_previous_handle(self):
        from flask import Flask
        from recost.frameworks.flask import RecostExtension

        ext = RecostExtension()
        try:
            app1 = Flask("app1")
            ext.init_app(app1, config=RecostConfig(enabled=True))
            first_handle = ext._handle
            assert first_handle is not None
            assert first_handle._disposed is False

            app2 = Flask("app2")
            ext.init_app(app2, config=RecostConfig(enabled=True))
            second_handle = ext._handle

            assert first_handle._disposed is True, (
                "previous handle was not disposed when init_app ran again"
            )
            assert second_handle is not first_handle
            assert second_handle is not None
            assert second_handle._disposed is False
        finally:
            uninstall()

    def test_init_app_idempotent_when_no_previous_handle(self):
        """First-time init_app on a fresh extension must work — the
        dispose-previous branch is skipped when self._handle is None."""
        from flask import Flask
        from recost.frameworks.flask import RecostExtension

        ext = RecostExtension()  # no app, self._handle stays None
        try:
            app = Flask(__name__)
            ext.init_app(app, config=RecostConfig(enabled=True))
            assert ext._handle is not None
            assert ext._handle._disposed is False
            assert is_installed()
        finally:
            uninstall()
