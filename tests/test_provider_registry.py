"""
Tests for ecoapi/_provider_registry.py

Covers: built-in provider matching, edge cases, custom provider priority,
wildcard hosts, Twilio refinement, BUILTIN_PROVIDERS array shape.

Ported from the Node SDK's provider-registry.test.ts.
"""

from ecoapi import ProviderRegistry, BUILTIN_PROVIDERS


class TestBuiltinProviders:
    """Tests for all 21 built-in provider rules."""

    def setup_method(self):
        self.registry = ProviderRegistry()

    # ── OpenAI ───────────────────────────────────────────────────────────

    def test_openai_chat_completions(self):
        result = self.registry.match("https://api.openai.com/v1/chat/completions")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "chat_completions"
        assert result.cost_per_request_cents == 2.0

    def test_openai_embeddings(self):
        result = self.registry.match("https://api.openai.com/v1/embeddings")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "embeddings"
        assert result.cost_per_request_cents == 0.01

    def test_openai_image_generation(self):
        result = self.registry.match("https://api.openai.com/v1/images/generations")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "image_generation"
        assert result.cost_per_request_cents == 4.0

    def test_openai_audio_transcription(self):
        result = self.registry.match("https://api.openai.com/v1/audio/transcriptions")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "audio_transcription"

    def test_openai_text_to_speech(self):
        result = self.registry.match("https://api.openai.com/v1/audio/speech")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "text_to_speech"

    def test_openai_catch_all(self):
        result = self.registry.match("https://api.openai.com/v1/some/future/endpoint")
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "/v1/some/future/endpoint"
        assert result.cost_per_request_cents == 1.0

    # ── Anthropic ────────────────────────────────────────────────────────

    def test_anthropic_messages(self):
        result = self.registry.match("https://api.anthropic.com/v1/messages")
        assert result is not None
        assert result.provider == "anthropic"
        assert result.endpoint_category == "messages"
        assert result.cost_per_request_cents == 1.5

    def test_anthropic_catch_all(self):
        result = self.registry.match("https://api.anthropic.com/v1/complete")
        assert result is not None
        assert result.provider == "anthropic"
        assert result.endpoint_category == "/v1/complete"

    # ── Stripe ───────────────────────────────────────────────────────────

    def test_stripe_charges(self):
        result = self.registry.match("https://api.stripe.com/v1/charges")
        assert result is not None
        assert result.provider == "stripe"
        assert result.endpoint_category == "charges"
        assert result.cost_per_request_cents == 0

    def test_stripe_payment_intents(self):
        result = self.registry.match("https://api.stripe.com/v1/payment_intents")
        assert result is not None
        assert result.provider == "stripe"
        assert result.endpoint_category == "payment_intents"

    def test_stripe_customers(self):
        result = self.registry.match("https://api.stripe.com/v1/customers")
        assert result is not None
        assert result.provider == "stripe"
        assert result.endpoint_category == "customers"

    def test_stripe_subscriptions(self):
        result = self.registry.match("https://api.stripe.com/v1/subscriptions")
        assert result is not None
        assert result.provider == "stripe"
        assert result.endpoint_category == "subscriptions"

    def test_stripe_catch_all(self):
        result = self.registry.match("https://api.stripe.com/v1/refunds")
        assert result is not None
        assert result.provider == "stripe"
        assert result.cost_per_request_cents == 0

    # ── Twilio ───────────────────────────────────────────────────────────

    def test_twilio_sms(self):
        result = self.registry.match(
            "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
        )
        assert result is not None
        assert result.provider == "twilio"
        assert result.endpoint_category == "sms"
        assert result.cost_per_request_cents == 0.79

    def test_twilio_voice_calls(self):
        result = self.registry.match(
            "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json"
        )
        assert result is not None
        assert result.provider == "twilio"
        assert result.endpoint_category == "voice_calls"
        assert result.cost_per_request_cents == 1.3

    def test_twilio_catch_all(self):
        result = self.registry.match(
            "https://api.twilio.com/2010-04-01/Accounts/AC123/Usage.json"
        )
        assert result is not None
        assert result.provider == "twilio"
        assert result.cost_per_request_cents == 0.5
        assert isinstance(result.endpoint_category, str)
        assert "/" in result.endpoint_category

    # ── SendGrid ─────────────────────────────────────────────────────────

    def test_sendgrid_mail_send(self):
        result = self.registry.match("https://api.sendgrid.com/v3/mail/send")
        assert result is not None
        assert result.provider == "sendgrid"
        assert result.endpoint_category == "send_email"
        assert result.cost_per_request_cents == 0.1

    def test_sendgrid_catch_all(self):
        result = self.registry.match("https://api.sendgrid.com/v3/templates")
        assert result is not None
        assert result.provider == "sendgrid"
        assert result.cost_per_request_cents == 0

    # ── Pinecone ─────────────────────────────────────────────────────────

    def test_pinecone_vector_upsert(self):
        result = self.registry.match(
            "https://my-index-abc.svc.pinecone.io/vectors/upsert"
        )
        assert result is not None
        assert result.provider == "pinecone"
        assert result.endpoint_category == "vector_upsert"
        assert result.cost_per_request_cents == 0.08

    def test_pinecone_query(self):
        result = self.registry.match(
            "https://my-index-abc.svc.us-east1-gcp.pinecone.io/query"
        )
        assert result is not None
        assert result.provider == "pinecone"
        assert result.endpoint_category == "vector_query"
        assert result.cost_per_request_cents == 0.08

    def test_pinecone_catch_all(self):
        result = self.registry.match(
            "https://my-index.svc.pinecone.io/describe_index_stats"
        )
        assert result is not None
        assert result.provider == "pinecone"
        assert result.cost_per_request_cents == 0.04

    # ── AWS ──────────────────────────────────────────────────────────────

    def test_aws_s3(self):
        result = self.registry.match(
            "https://s3.us-east-1.amazonaws.com/bucket/key"
        )
        assert result is not None
        assert result.provider == "aws"
        assert result.cost_per_request_cents == 0

    def test_aws_lambda(self):
        result = self.registry.match(
            "https://lambda.us-west-2.amazonaws.com/2015-03-31/functions"
        )
        assert result is not None
        assert result.provider == "aws"

    # ── Google Cloud ─────────────────────────────────────────────────────

    def test_gcp_storage(self):
        result = self.registry.match(
            "https://storage.googleapis.com/bucket/object"
        )
        assert result is not None
        assert result.provider == "gcp"
        assert result.cost_per_request_cents == 0

    def test_gcp_bigquery(self):
        result = self.registry.match(
            "https://bigquery.googleapis.com/bigquery/v2/projects"
        )
        assert result is not None
        assert result.provider == "gcp"


class TestEdgeCases:
    def setup_method(self):
        self.registry = ProviderRegistry()

    def test_unknown_host_returns_none(self):
        assert self.registry.match("https://my-internal-api.company.com/users") is None

    def test_malformed_url_returns_none(self):
        assert self.registry.match("not-a-url") is None

    def test_empty_string_returns_none(self):
        assert self.registry.match("") is None

    def test_query_params_stripped(self):
        result = self.registry.match(
            "https://api.openai.com/v1/chat/completions?model=gpt-4&stream=true"
        )
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "chat_completions"

    def test_explicit_port_443(self):
        result = self.registry.match(
            "https://api.openai.com:443/v1/chat/completions"
        )
        assert result is not None
        assert result.provider == "openai"
        assert result.endpoint_category == "chat_completions"

    def test_trailing_slash_on_path_prefix(self):
        result = self.registry.match(
            "https://api.anthropic.com/v1/messages/batch"
        )
        assert result is not None
        assert result.provider == "anthropic"
        assert result.endpoint_category == "messages"


class TestCustomProviders:
    def test_custom_takes_priority(self):
        registry = ProviderRegistry([
            ProviderDef(
                host_pattern="api.openai.com",
                path_prefix="/v1/chat",
                provider="custom-openai",
                endpoint_category="custom_chat",
                cost_per_request_cents=99,
            ),
        ])
        result = registry.match("https://api.openai.com/v1/chat/completions")
        assert result is not None
        assert result.provider == "custom-openai"
        assert result.endpoint_category == "custom_chat"
        assert result.cost_per_request_cents == 99

    def test_custom_new_host(self):
        registry = ProviderRegistry([
            ProviderDef(
                host_pattern="api.acme.com",
                path_prefix="/payments",
                provider="acme",
                endpoint_category="charge",
                cost_per_request_cents=0.5,
            ),
        ])
        result = registry.match("https://api.acme.com/payments/create")
        assert result is not None
        assert result.provider == "acme"
        assert result.endpoint_category == "charge"
        assert result.cost_per_request_cents == 0.5

    def test_custom_without_path_prefix(self):
        registry = ProviderRegistry([
            ProviderDef(
                host_pattern="internal.api.com",
                provider="internal",
                endpoint_category="any",
                cost_per_request_cents=0,
            ),
        ])
        assert registry.match("https://internal.api.com/users") is not None
        assert registry.match("https://internal.api.com/users").provider == "internal"
        assert registry.match("https://internal.api.com/orders/123").provider == "internal"
        assert registry.match("https://internal.api.com/").provider == "internal"

    def test_custom_does_not_affect_builtins(self):
        registry = ProviderRegistry([
            ProviderDef(host_pattern="api.acme.com", provider="acme"),
        ])
        assert registry.match("https://api.openai.com/v1/embeddings").provider == "openai"
        assert registry.match("https://api.stripe.com/v1/charges").provider == "stripe"
        assert registry.match("https://unknown.example.com/") is None

    def test_custom_appears_first_in_list(self):
        registry = ProviderRegistry([
            ProviderDef(host_pattern="api.acme.com", provider="acme"),
        ])
        rules = registry.list()
        assert rules[0].provider == "acme"
        assert any(r.provider == "openai" for r in rules)


class TestBuiltinProvidersArray:
    def test_has_21_rules(self):
        assert len(BUILTIN_PROVIDERS) == 21

    def test_all_rules_have_host_and_provider(self):
        for rule in BUILTIN_PROVIDERS:
            assert isinstance(rule.host_pattern, str)
            assert len(rule.host_pattern) > 0
            assert isinstance(rule.provider, str)
            assert len(rule.provider) > 0

    def test_openai_specific_before_catch_all(self):
        openai_rules = [
            (i, r) for i, r in enumerate(BUILTIN_PROVIDERS) if r.provider == "openai"
        ]
        catch_all = [(i, r) for i, r in openai_rules if r.path_prefix is None]
        specific = [(i, r) for i, r in openai_rules if r.path_prefix is not None]
        assert len(catch_all) > 0
        assert len(specific) > 0
        for s_idx, _ in specific:
            assert s_idx < catch_all[0][0]

    def test_stripe_specific_before_catch_all(self):
        stripe_rules = [
            (i, r) for i, r in enumerate(BUILTIN_PROVIDERS) if r.provider == "stripe"
        ]
        catch_all = [(i, r) for i, r in stripe_rules if r.path_prefix is None]
        specific = [(i, r) for i, r in stripe_rules if r.path_prefix is not None]
        assert len(catch_all) > 0
        for s_idx, _ in specific:
            assert s_idx < catch_all[0][0]

    def test_pinecone_specific_before_catch_all(self):
        pinecone_rules = [
            (i, r) for i, r in enumerate(BUILTIN_PROVIDERS) if r.provider == "pinecone"
        ]
        catch_all = [(i, r) for i, r in pinecone_rules if r.path_prefix is None]
        specific = [(i, r) for i, r in pinecone_rules if r.path_prefix is not None]
        assert len(catch_all) > 0
        for s_idx, _ in specific:
            assert s_idx < catch_all[0][0]


# Need to import ProviderDef for custom provider tests
from ecoapi import ProviderDef  # noqa: E402
