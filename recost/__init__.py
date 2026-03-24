"""
recost — Python SDK for ReCost.

Tracks outbound HTTP API calls and reports cost, latency, and usage patterns
to the ReCost dashboard or your local VS Code extension.
"""

from ._types import (
    RecostConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    TransportMode,
    WindowSummary,
)
from ._init import RecostHandle, init
from ._provider_registry import BUILTIN_PROVIDERS, MatchResult, ProviderRegistry
from ._interceptor import install, uninstall, is_installed
from ._aggregator import Aggregator

__all__ = [
    "init",
    "RecostHandle",
    "RawEvent",
    "MetricEntry",
    "WindowSummary",
    "ProviderDef",
    "RecostConfig",
    "TransportMode",
    "ProviderRegistry",
    "BUILTIN_PROVIDERS",
    "MatchResult",
    "install",
    "uninstall",
    "is_installed",
    "Aggregator",
]
