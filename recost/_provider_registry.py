"""
ProviderRegistry — matches intercepted request URLs to known API providers.

Rules are checked in order; the first match wins. Custom providers are
prepended at construction time so they always take priority over built-ins.

Direct port of the Node SDK's provider-registry.ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from ._types import ProviderDef


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """The result of a successful registry lookup for a given URL."""

    provider: str
    endpoint_category: str
    cost_per_request_cents: float


# ---------------------------------------------------------------------------
# Built-in provider definitions (same 21 rules as Node SDK, same order)
# ---------------------------------------------------------------------------

BUILTIN_PROVIDERS: List[ProviderDef] = [
    # ── OpenAI ───────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.openai.com", path_prefix="/v1/chat/completions",     provider="openai", endpoint_category="chat_completions",    cost_per_request_cents=2.0),
    ProviderDef(host_pattern="api.openai.com", path_prefix="/v1/embeddings",           provider="openai", endpoint_category="embeddings",          cost_per_request_cents=0.01),
    ProviderDef(host_pattern="api.openai.com", path_prefix="/v1/images/generations",   provider="openai", endpoint_category="image_generation",    cost_per_request_cents=4.0),
    ProviderDef(host_pattern="api.openai.com", path_prefix="/v1/audio/transcriptions", provider="openai", endpoint_category="audio_transcription", cost_per_request_cents=0.6),
    ProviderDef(host_pattern="api.openai.com", path_prefix="/v1/audio/speech",         provider="openai", endpoint_category="text_to_speech",      cost_per_request_cents=1.5),
    ProviderDef(host_pattern="api.openai.com",                                         provider="openai",                                          cost_per_request_cents=1.0),

    # ── Anthropic ────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.anthropic.com", path_prefix="/v1/messages", provider="anthropic", endpoint_category="messages", cost_per_request_cents=1.5),
    ProviderDef(host_pattern="api.anthropic.com",                              provider="anthropic",                               cost_per_request_cents=1.0),

    # ── Stripe ───────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.stripe.com", path_prefix="/v1/charges",         provider="stripe", endpoint_category="charges",         cost_per_request_cents=0),
    ProviderDef(host_pattern="api.stripe.com", path_prefix="/v1/payment_intents", provider="stripe", endpoint_category="payment_intents", cost_per_request_cents=0),
    ProviderDef(host_pattern="api.stripe.com", path_prefix="/v1/customers",       provider="stripe", endpoint_category="customers",       cost_per_request_cents=0),
    ProviderDef(host_pattern="api.stripe.com", path_prefix="/v1/subscriptions",   provider="stripe", endpoint_category="subscriptions",   cost_per_request_cents=0),
    ProviderDef(host_pattern="api.stripe.com",                                     provider="stripe",                                      cost_per_request_cents=0),

    # ── Twilio ───────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.twilio.com", provider="twilio", cost_per_request_cents=0.5),

    # ── SendGrid ─────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.sendgrid.com", path_prefix="/v3/mail/send", provider="sendgrid", endpoint_category="send_email", cost_per_request_cents=0.1),
    ProviderDef(host_pattern="api.sendgrid.com",                               provider="sendgrid",                                cost_per_request_cents=0),

    # ── Pinecone ─────────────────────────────────────────────────────────
    ProviderDef(host_pattern="*.pinecone.io", path_prefix="/vectors/upsert", provider="pinecone", endpoint_category="vector_upsert", cost_per_request_cents=0.08),
    ProviderDef(host_pattern="*.pinecone.io", path_prefix="/query",          provider="pinecone", endpoint_category="vector_query",  cost_per_request_cents=0.08),
    ProviderDef(host_pattern="*.pinecone.io",                                 provider="pinecone",                                    cost_per_request_cents=0.04),

    # ── AWS ──────────────────────────────────────────────────────────────
    ProviderDef(host_pattern="*.amazonaws.com", provider="aws", cost_per_request_cents=0),

    # ── Google Cloud ─────────────────────────────────────────────────────
    ProviderDef(host_pattern="*.googleapis.com", provider="gcp", cost_per_request_cents=0),

    # ── GitHub ───────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.github.com", path_prefix="/repos",  provider="github", endpoint_category="repos",  cost_per_request_cents=0),
    ProviderDef(host_pattern="api.github.com", path_prefix="/users",  provider="github", endpoint_category="users",  cost_per_request_cents=0),
    ProviderDef(host_pattern="api.github.com", path_prefix="/search", provider="github", endpoint_category="search", cost_per_request_cents=0),
    ProviderDef(host_pattern="api.github.com",                         provider="github",                             cost_per_request_cents=0),

    # ── CoinGecko ────────────────────────────────────────────────────────
    ProviderDef(host_pattern="api.coingecko.com", path_prefix="/api/v3/simple/price", provider="coingecko", endpoint_category="simple_price", cost_per_request_cents=0),
    ProviderDef(host_pattern="api.coingecko.com", path_prefix="/api/v3/coins",        provider="coingecko", endpoint_category="coins",         cost_per_request_cents=0),
    ProviderDef(host_pattern="api.coingecko.com",                                      provider="coingecko",                                    cost_per_request_cents=0),

    # ── Hacker News ──────────────────────────────────────────────────────
    ProviderDef(host_pattern="hacker-news.firebaseio.com", path_prefix="/v0/topstories", provider="hackernews", endpoint_category="topstories", cost_per_request_cents=0),
    ProviderDef(host_pattern="hacker-news.firebaseio.com", path_prefix="/v0/item",       provider="hackernews", endpoint_category="item",       cost_per_request_cents=0),
    ProviderDef(host_pattern="hacker-news.firebaseio.com",                                provider="hackernews",                                 cost_per_request_cents=0),

    # ── wttr.in (weather) ────────────────────────────────────────────────
    ProviderDef(host_pattern="wttr.in", provider="wttr", endpoint_category="weather", cost_per_request_cents=0),

    # ── ZenQuotes ────────────────────────────────────────────────────────
    ProviderDef(host_pattern="zenquotes.io", provider="zenquotes", endpoint_category="random_quote", cost_per_request_cents=0),

    # ── ip-api (geolocation) ─────────────────────────────────────────────
    ProviderDef(host_pattern="ip-api.com", provider="ip-api", endpoint_category="geolocation", cost_per_request_cents=0),
]


# ---------------------------------------------------------------------------
# Host matching helper
# ---------------------------------------------------------------------------

def _host_matches(pattern: str, hostname: str) -> bool:
    if pattern.startswith("*."):
        # Wildcard: "*.amazonaws.com" matches "s3.us-east-1.amazonaws.com"
        return hostname.endswith(pattern[1:])  # pattern[1:] → ".amazonaws.com"
    return hostname == pattern


# ---------------------------------------------------------------------------
# Twilio path refinement
# ---------------------------------------------------------------------------

def _refine_twilio(pathname: str) -> tuple:
    """Refine category and cost for Twilio after a host-level match."""
    if "/Messages" in pathname:
        return "sms", 0.79
    if "/Calls" in pathname:
        return "voice_calls", 1.3
    return pathname, 0.5


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Maps intercepted request URLs to provider metadata using an ordered rule list."""

    def __init__(self, custom_providers: Optional[List[ProviderDef]] = None) -> None:
        self._rules: List[ProviderDef] = [*(custom_providers or []), *BUILTIN_PROVIDERS]

    def match(self, url: str) -> Optional[MatchResult]:
        """
        Match a full URL string against the rule list.
        Returns the first matching MatchResult, or None if no rule applies.
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        hostname = parsed.hostname or ""
        pathname = parsed.path or "/"

        if not hostname:
            return None

        for rule in self._rules:
            if not _host_matches(rule.host_pattern, hostname):
                continue
            if rule.path_prefix is not None and not pathname.startswith(rule.path_prefix):
                continue

            # Host (and optional path) matched — build the result
            endpoint_category = rule.endpoint_category if rule.endpoint_category is not None else pathname
            cost_per_request_cents = rule.cost_per_request_cents if rule.cost_per_request_cents is not None else 0.0

            # Post-match refinement for providers with dynamic path structures
            if rule.provider == "twilio" and rule.endpoint_category is None:
                endpoint_category, cost_per_request_cents = _refine_twilio(pathname)

            return MatchResult(
                provider=rule.provider,
                endpoint_category=endpoint_category,
                cost_per_request_cents=cost_per_request_cents,
            )

        return None

    def list(self) -> List[ProviderDef]:
        """Returns all rules in priority order (custom first, built-ins after)."""
        return list(self._rules)
