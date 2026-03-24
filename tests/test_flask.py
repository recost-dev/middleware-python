"""
Tests for recost/frameworks/flask.py
"""

import pytest

from recost._interceptor import is_installed, uninstall
from recost._types import RecostConfig


class TestFlaskExtension:
    def test_extension_initializes_interceptor(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import ReCost

            app = Flask(__name__)
            eco = ReCost(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_init_app_pattern(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import ReCost

            eco = ReCost()
            app = Flask(__name__)
            eco.init_app(app, config=RecostConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_accepts_kwargs(self):
        try:
            from flask import Flask
            from recost.frameworks.flask import ReCost

            app = Flask(__name__)
            eco = ReCost(app, enabled=True, debug=False)
            assert is_installed()
        finally:
            uninstall()
