"""Shared fixtures for all recost tests."""

import pytest
from recost._interceptor import uninstall


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure interceptor is uninstalled after each test."""
    yield
    uninstall()
