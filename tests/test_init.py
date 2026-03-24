"""
Tests for recost/_init.py
"""

import threading
import time

import pytest

from recost._init import init
from recost._interceptor import is_installed, uninstall
from recost._types import RecostConfig


class TestInit:
    def test_install_and_dispose(self):
        handle = init(RecostConfig(enabled=True))
        assert is_installed()
        handle.dispose()
        assert not is_installed()

    def test_disabled_does_not_install(self):
        handle = init(RecostConfig(enabled=False))
        assert not is_installed()
        handle.dispose()

    def test_double_init_disposes_first(self):
        h1 = init(RecostConfig())
        assert is_installed()
        h2 = init(RecostConfig())
        assert is_installed()
        h2.dispose()
        assert not is_installed()

    def test_dispose_is_idempotent(self):
        handle = init(RecostConfig())
        handle.dispose()
        handle.dispose()  # Should not raise
        assert not is_installed()


class TestExcludePatterns:
    def test_cloud_mode_excludes_base_url(self):
        # We can't easily test the filtering without making real requests,
        # but we can verify init() doesn't crash with these settings
        handle = init(RecostConfig(
            api_key="test",
            project_id="proj",
            base_url="https://api.recost.dev",
            exclude_patterns=["/favicon.ico"],
        ))
        assert is_installed()
        handle.dispose()

    def test_local_mode_excludes_localhost(self):
        handle = init(RecostConfig(
            local_port=9999,
        ))
        assert is_installed()
        handle.dispose()
