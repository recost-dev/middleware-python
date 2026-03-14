"""Shared fixtures for all ecoapi tests."""

import pytest
from ecoapi._interceptor import uninstall


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure interceptor is uninstalled after each test."""
    yield
    uninstall()
