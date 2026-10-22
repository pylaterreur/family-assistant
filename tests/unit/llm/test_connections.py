"""Model clients borrow provider SDK clients from a shared registry."""

import pytest

from family_assistant.llm.connections import ConnectionKey, ProviderConnections
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.retrying_client import RetryingLLMClient


def _openai(
    connections: ProviderConnections | None, model: str, **config: object
) -> OpenAIClient:
    client = LLMClientFactory.create_client(
        {"model": model, "provider": "openai", "api_key": "sk-test", **config},
        connections=connections,
    )
    assert isinstance(client, OpenAIClient)
    return client


def _google(
    connections: ProviderConnections | None, model: str, **config: object
) -> GoogleGenAIClient:
    client = LLMClientFactory.create_client(
        {"model": model, "provider": "google", "api_key": "g-test", **config},
        connections=connections,
    )
    assert isinstance(client, GoogleGenAIClient)
    return client


def test_clients_on_one_endpoint_share_an_sdk_client() -> None:
    connections = ProviderConnections()

    luna = _openai(connections, "gpt-6-luna")
    terra = _openai(connections, "gpt-5.6-terra")
    flash = _google(connections, "gemini-3.8-flash")
    argon = _google(connections, "gemini-4-argon")

    assert luna.client is terra.client
    assert flash.client is argon.client
    assert len(connections) == 2


def test_endpoint_and_credential_separate_connections() -> None:
    connections = ProviderConnections()

    direct = _openai(connections, "gpt-6-luna")
    other_key = _openai(connections, "gpt-6-luna", api_key="sk-other")
    openrouter = _openai(connections, "openrouter/google/gemini-3.8-flash")

    assert direct.client is not other_key.client
    assert direct.client is not openrouter.client
    assert len(connections) == 3


def test_retry_chain_entries_share_a_connection() -> None:
    connections = ProviderConnections()
    client = LLMClientFactory.create_client(
        {
            "retry_config": {
                "primary": {
                    "model": "gpt-6-luna",
                    "provider": "openai",
                    "api_key": "sk-test",
                },
                "fallback": {
                    "model": "gpt-5.6-terra",
                    "provider": "openai",
                    "api_key": "sk-test",
                },
            }
        },
        connections=connections,
    )

    assert isinstance(client, RetryingLLMClient)
    assert isinstance(client.primary_client, OpenAIClient)
    assert isinstance(client.fallback_client, OpenAIClient)
    assert client.primary_client.client is client.fallback_client.client


def test_without_a_registry_each_client_owns_its_sdk_client() -> None:
    first = _openai(None, "gpt-6-luna")
    second = _openai(None, "gpt-6-luna")

    assert first.client is not second.client


def test_record_replay_google_client_is_never_shared(tmp_path: object) -> None:
    connections = ProviderConnections()
    shared = _google(connections, "gemini-3.8-flash")
    replaying = _google(
        connections,
        "gemini-3.8-flash",
        debug_config={
            "client_mode": "replay",
            "replay_id": "unused",
            "replays_directory": str(tmp_path),
        },
    )

    assert replaying.client is not shared.client
    assert len(connections) == 1


@pytest.mark.asyncio
async def test_closing_a_borrowing_client_leaves_the_connection_open() -> None:
    connections = ProviderConnections()
    luna = _openai(connections, "gpt-6-luna")
    terra = _openai(connections, "gpt-5.6-terra")
    anthropic_client = LLMClientFactory.create_client(
        {"model": "claude-haiku-5-5", "provider": "anthropic", "api_key": "a-test"},
        connections=connections,
    )
    assert isinstance(anthropic_client, AnthropicClient)

    await luna.close()
    await anthropic_client.close()

    assert not terra.client.is_closed()
    assert not anthropic_client.client.is_closed()

    await connections.aclose()

    assert terra.client.is_closed()
    assert anthropic_client.client.is_closed()
    assert len(connections) == 0


@pytest.mark.asyncio
async def test_closing_an_owning_client_closes_its_sdk_client() -> None:
    client = _openai(None, "gpt-6-luna")

    await client.close()

    assert client.client.is_closed()


@pytest.mark.asyncio
async def test_aclose_attempts_every_connection_and_raises_the_first_error() -> None:
    connections = ProviderConnections()
    closed: list[str] = []

    def failing_close(name: str) -> None:
        closed.append(name)
        raise RuntimeError(name)

    async def async_close(name: str) -> None:
        closed.append(name)

    connections.get(ConnectionKey("a", None, "k"), lambda: "first", failing_close)
    connections.get(ConnectionKey("b", None, "k"), lambda: "second", async_close)

    with pytest.raises(RuntimeError, match="first"):
        await connections.aclose()

    assert closed == ["first", "second"]


def test_connection_key_repr_omits_the_credential() -> None:
    key = ConnectionKey("openai", "https://example.test/v1", "sk-secret")

    assert "sk-secret" not in repr(key)
