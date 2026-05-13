"""
Tests for recost/frameworks/flask.py
"""

import warnings

import pytest

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
