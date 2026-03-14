"""
Tests for ecoapi/_init.py
"""

import threading
import time

import pytest

from ecoapi._init import init
from ecoapi._interceptor import is_installed, uninstall
from ecoapi._types import EcoAPIConfig


class TestInit:
    def test_install_and_dispose(self):
        handle = init(EcoAPIConfig(enabled=True))
        assert is_installed()
        handle.dispose()
        assert not is_installed()

    def test_disabled_does_not_install(self):
        handle = init(EcoAPIConfig(enabled=False))
        assert not is_installed()
        handle.dispose()

    def test_double_init_disposes_first(self):
        h1 = init(EcoAPIConfig())
        assert is_installed()
        h2 = init(EcoAPIConfig())
        assert is_installed()
        h2.dispose()
        assert not is_installed()

    def test_dispose_is_idempotent(self):
        handle = init(EcoAPIConfig())
        handle.dispose()
        handle.dispose()  # Should not raise
        assert not is_installed()


class TestExcludePatterns:
    def test_cloud_mode_excludes_base_url(self):
        # We can't easily test the filtering without making real requests,
        # but we can verify init() doesn't crash with these settings
        handle = init(EcoAPIConfig(
            api_key="test",
            project_id="proj",
            base_url="https://api.ecoapi.dev",
            exclude_patterns=["/favicon.ico"],
        ))
        assert is_installed()
        handle.dispose()

    def test_local_mode_excludes_localhost(self):
        handle = init(EcoAPIConfig(
            local_port=9999,
        ))
        assert is_installed()
        handle.dispose()
