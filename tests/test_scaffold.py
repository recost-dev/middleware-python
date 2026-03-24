"""
Scaffold smoke tests — verifies the package exports resolve without errors.
"""

from recost import (
    Aggregator,
    BUILTIN_PROVIDERS,
    ProviderRegistry,
    is_installed,
    uninstall,
)
from recost._transport import Transport
from recost._types import RecostConfig


class TestScaffold:
    def test_provider_registry_instantiates(self):
        registry = ProviderRegistry()
        assert registry is not None
        assert len(registry.list()) > 0

    def test_interceptor_not_installed_by_default(self):
        assert is_installed() is False

    def test_aggregator_instantiates_with_size_zero(self):
        agg = Aggregator()
        assert agg is not None
        assert agg.size == 0

    def test_transport_local_mode_when_no_key(self):
        transport = Transport(RecostConfig())
        assert transport.mode == "local"
        transport.dispose()

    def test_transport_cloud_mode_when_key_set(self):
        transport = Transport(RecostConfig(api_key="test-key"))
        assert transport.mode == "cloud"
        transport.dispose()
