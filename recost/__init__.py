"""
recost — Python SDK for Recost.

Tracks outbound HTTP API calls and reports cost, latency, and usage patterns
to the Recost dashboard or your local VS Code extension.
"""

from ._types import (
    FlushStatus,
    RecostConfig,
    MetricEntry,
    ProviderDef,
    RawEvent,
    RecostError,
    RecostAuthError,
    RecostFatalAuthError,
    RecostRateLimitError,
    TransportMode,
    WindowSummary,
)
from ._init import RecostHandle, init
from ._provider_registry import BUILTIN_PROVIDERS, MatchResult, ProviderRegistry
from ._interceptor import install, uninstall, is_installed
from ._aggregator import Aggregator, MAX_BUCKETS

__all__ = [
    "init",
    "RecostHandle",
    "RawEvent",
    "MetricEntry",
    "WindowSummary",
    "ProviderDef",
    "RecostConfig",
    "RecostError",
    "RecostAuthError",
    "RecostFatalAuthError",
    "RecostRateLimitError",
    "TransportMode",
    "FlushStatus",
    "ProviderRegistry",
    "BUILTIN_PROVIDERS",
    "MatchResult",
    "install",
    "uninstall",
    "is_installed",
    "Aggregator",
    "MAX_BUCKETS",
]
