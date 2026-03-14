"""
Tests for ecoapi/frameworks/fastapi.py
"""

import pytest

from ecoapi._interceptor import is_installed, uninstall
from ecoapi._types import EcoAPIConfig


class TestFastAPIMiddleware:
    def test_middleware_initializes_interceptor(self):
        try:
            from starlette.applications import Starlette
            from starlette.responses import PlainTextResponse
            from starlette.routing import Route
            from starlette.testclient import TestClient
            from ecoapi.frameworks.fastapi import EcoAPIMiddleware

            def homepage(request):
                return PlainTextResponse("ok")

            app = Starlette(routes=[Route("/", homepage)])
            app.add_middleware(EcoAPIMiddleware, config=EcoAPIConfig(enabled=True))
            client = TestClient(app)
            # Trigger a request so middleware is instantiated
            resp = client.get("/")
            assert resp.status_code == 200
            assert is_installed()
        finally:
            uninstall()

    def test_middleware_accepts_kwargs(self):
        try:
            from starlette.applications import Starlette
            from starlette.responses import PlainTextResponse
            from starlette.routing import Route
            from starlette.testclient import TestClient
            from ecoapi.frameworks.fastapi import EcoAPIMiddleware

            def homepage(request):
                return PlainTextResponse("ok")

            app = Starlette(routes=[Route("/", homepage)])
            app.add_middleware(EcoAPIMiddleware, enabled=True, debug=False)
            client = TestClient(app)
            resp = client.get("/")
            assert resp.status_code == 200
            assert is_installed()
        finally:
            uninstall()
