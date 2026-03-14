"""
ecoapi — Python SDK for EcoAPI.

Tracks outbound HTTP API calls and reports cost, latency, and usage patterns
to the EcoAPI dashboard or your local VS Code extension.
"""

from ._types import (
    EcoAPIConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    TransportMode,
    WindowSummary,
)
from ._init import EcoAPIHandle, init
from ._provider_registry import BUILTIN_PROVIDERS, MatchResult, ProviderRegistry
from ._interceptor import install, uninstall, is_installed
from ._aggregator import Aggregator

__all__ = [
    "init",
    "EcoAPIHandle",
    "RawEvent",
    "MetricEntry",
    "WindowSummary",
    "ProviderDef",
    "EcoAPIConfig",
    "TransportMode",
    "ProviderRegistry",
    "BUILTIN_PROVIDERS",
    "MatchResult",
    "install",
    "uninstall",
    "is_installed",
    "Aggregator",
]
