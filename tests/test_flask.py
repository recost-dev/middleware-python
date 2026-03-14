"""
Tests for ecoapi/frameworks/flask.py
"""

import pytest

from ecoapi._interceptor import is_installed, uninstall
from ecoapi._types import EcoAPIConfig


class TestFlaskExtension:
    def test_extension_initializes_interceptor(self):
        try:
            from flask import Flask
            from ecoapi.frameworks.flask import EcoAPI

            app = Flask(__name__)
            eco = EcoAPI(app, config=EcoAPIConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_init_app_pattern(self):
        try:
            from flask import Flask
            from ecoapi.frameworks.flask import EcoAPI

            eco = EcoAPI()
            app = Flask(__name__)
            eco.init_app(app, config=EcoAPIConfig(enabled=True))
            assert is_installed()
        finally:
            uninstall()

    def test_extension_accepts_kwargs(self):
        try:
            from flask import Flask
            from ecoapi.frameworks.flask import EcoAPI

            app = Flask(__name__)
            eco = EcoAPI(app, enabled=True, debug=False)
            assert is_installed()
        finally:
            uninstall()
