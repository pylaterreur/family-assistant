"""``flex`` on a chain entry is honoured, or refused when the config loads."""

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig, ModelTierConfig, RetryModelConfig
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.flex import FlexClient
from family_assistant.llm.model_tiers import resolve_tier_client_config
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.retrying_client import RetryingLLMClient


def _config(model: str, provider: str, **extra: object) -> dict[str, object]:
    return {"model": model, "provider": provider, "api_key": "test-key", **extra}


def test_a_flex_entry_is_wrapped_around_its_provider_client() -> None:
    client = LLMClientFactory.create_client(
        _config("gemini-3.8-flash", "google", flex=True)
    )

    assert isinstance(client, FlexClient)
    assert isinstance(client.inner, GoogleGenAIClient)


def test_an_entry_without_flex_is_the_bare_provider_client() -> None:
    client = LLMClientFactory.create_client(_config("gpt-6-luna", "openai"))

    assert isinstance(client, OpenAIClient)


def test_flex_applies_to_its_own_chain_entry_only() -> None:
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(provider="google", model="gemini-3.8-flash", flex=True),
            RetryModelConfig(provider="anthropic", model="claude-haiku-5-5"),
        ]
    )
    config = resolve_tier_client_config(tier, {})
    for entry in config["retry_config"].values():
        entry["api_key"] = "test-key"

    client = LLMClientFactory.create_client(config)

    assert isinstance(client, RetryingLLMClient)
    assert isinstance(client.primary_client, FlexClient)
    assert not isinstance(client.fallback_client, FlexClient)


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        pytest.param(
            _config("claude-haiku-5-5", "anthropic", flex=True),
            "provider 'anthropic' has no Flex",
            id="anthropic",
        ),
        pytest.param(
            _config("deep-research-pro-preview-12-2025", "google", flex=True),
            "Interactions API agent",
            id="interactions-agent",
        ),
        pytest.param(
            _config(
                "gemini-3.8-flash",
                "google",
                flex=True,
                model_parameters={"gemini-": {"service_tier": "standard"}},
            ),
            "also configures service_tier",
            id="explicit-service-tier",
        ),
    ],
)
def test_a_flex_entry_its_client_cannot_honour_is_refused(
    config: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        LLMClientFactory.validate_flex_config(config)


def test_the_factory_refuses_it_too() -> None:
    with pytest.raises(ValueError, match="has no Flex"):
        LLMClientFactory.create_client(
            _config("claude-haiku-5-5", "anthropic", flex=True)
        )


def test_startup_refuses_flex_on_a_tier_no_profile_reaches() -> None:
    config = AppConfig(
        model_tiers={
            "unused": ModelTierConfig(
                chain=[
                    RetryModelConfig(
                        provider="anthropic", model="claude-opus-5-5", flex=True
                    )
                ]
            )
        }
    )

    with pytest.raises(SystemExit, match=r"model_tiers\.unused: .*has no Flex"):
        Assistant(config)._validate_flex_tier_entries()
