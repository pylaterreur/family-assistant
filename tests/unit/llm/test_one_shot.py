"""Unit tests for one_shot LLM utilities."""

import json
from collections.abc import Mapping, Sequence

import pytest
from pydantic import BaseModel

from family_assistant.llm import LLMInterface, LLMOutput
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import LLMMessage
from family_assistant.llm.one_shot import (
    DEFAULT_MODEL,
    one_shot,
    one_shot_json,
    one_shot_structured,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class SummaryModel(BaseModel):
    """Model for structured output tests."""

    title: str
    key_points: list[str]


class RecordingClientFactory:
    """Stands in for ``LLMClientFactory.create_client``, returning one client."""

    def __init__(self, client: LLMInterface) -> None:
        self.client = client
        self.configs: list[Mapping[str, object]] = []

    def __call__(self, config: Mapping[str, object]) -> LLMInterface:
        self.configs.append(config)
        return self.client


def install_client(
    monkeypatch: pytest.MonkeyPatch, client: RuleBasedMockLLMClient
) -> RecordingClientFactory:
    factory = RecordingClientFactory(client)
    monkeypatch.setattr(LLMClientFactory, "create_client", factory)
    return factory


def text_client(content: str | None = "Test response") -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=content))


def structured_client(result: BaseModel) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[], structured_rules=[(lambda _kwargs: True, result)]
    )


def messages_sent(client: RuleBasedMockLLMClient, method: str) -> Sequence[LLMMessage]:
    calls = [c for c in client.get_calls() if c["method_name"] == method]
    assert len(calls) == 1
    return calls[0]["kwargs"]["messages"]


@pytest.mark.no_db
async def test_one_shot_simple(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test basic one_shot call."""
    client = text_client()
    factory = install_client(monkeypatch, client)

    result = await one_shot("Hello, world!")

    assert factory.configs == [{"model": DEFAULT_MODEL}]
    messages = messages_sent(client, "generate_response")
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "Hello, world!"
    assert result == "Test response"


@pytest.mark.no_db
async def test_one_shot_with_system_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one_shot with system message."""
    client = text_client()
    install_client(monkeypatch, client)

    await one_shot(
        "Hello",
        system="You are a helpful assistant.",
    )

    messages = messages_sent(client, "generate_response")
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content == "You are a helpful assistant."
    assert messages[1].role == "user"
    assert messages[1].content == "Hello"


@pytest.mark.no_db
async def test_one_shot_with_custom_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one_shot with custom model."""
    factory = install_client(monkeypatch, text_client())

    await one_shot("Hello", model="gpt-4o")

    assert factory.configs == [{"model": "gpt-4o"}]


@pytest.mark.no_db
async def test_one_shot_raises_on_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test one_shot raises ValueError when LLM returns no content."""
    install_client(monkeypatch, text_client(content=None))

    with pytest.raises(ValueError, match="LLM returned no content"):
        await one_shot("Hello")


@pytest.mark.no_db
async def test_one_shot_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one_shot_structured call."""
    expected_result = SummaryModel(title="Test", key_points=["Point 1", "Point 2"])
    client = structured_client(expected_result)
    factory = install_client(monkeypatch, client)

    result = await one_shot_structured(
        "Summarize this",
        response_model=SummaryModel,
    )

    assert factory.configs == [{"model": DEFAULT_MODEL}]
    calls = [c for c in client.get_calls() if c["method_name"] == "generate_structured"]
    assert len(calls) == 1
    messages = calls[0]["kwargs"]["messages"]
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert calls[0]["kwargs"]["response_model"] is SummaryModel
    assert result == expected_result
    assert result.title == "Test"
    assert result.key_points == ["Point 1", "Point 2"]


@pytest.mark.no_db
async def test_one_shot_structured_with_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test one_shot_structured with system message."""
    client = structured_client(SummaryModel(title="Test", key_points=[]))
    install_client(monkeypatch, client)

    await one_shot_structured(
        "Summarize this",
        response_model=SummaryModel,
        system="You are a summarizer.",
    )

    messages = messages_sent(client, "generate_structured")
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content == "You are a summarizer."
    assert messages[1].role == "user"


@pytest.mark.no_db
async def test_one_shot_structured_with_custom_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test one_shot_structured with custom model."""
    factory = install_client(
        monkeypatch, structured_client(SummaryModel(title="Test", key_points=[]))
    )

    await one_shot_structured(
        "Summarize this",
        response_model=SummaryModel,
        model="claude-3-sonnet",
    )

    assert factory.configs == [{"model": "claude-3-sonnet"}]


@pytest.mark.no_db
async def test_one_shot_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one_shot_json call."""
    expected_result = {"name": "Alice", "age": 30}
    client = text_client(json.dumps(expected_result))
    factory = install_client(monkeypatch, client)

    result = await one_shot_json("Extract info from: Alice is 30 years old")

    assert factory.configs == [{"model": DEFAULT_MODEL}]
    messages = messages_sent(client, "generate_json")
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert result == expected_result


@pytest.mark.no_db
async def test_one_shot_json_with_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one_shot_json with system message."""
    client = text_client('{"key": "value"}')
    install_client(monkeypatch, client)

    await one_shot_json(
        "Extract info",
        system="Be concise.",
    )

    messages = messages_sent(client, "generate_json")
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content == "Be concise."
    assert messages[1].role == "user"


@pytest.mark.no_db
async def test_one_shot_json_with_custom_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test one_shot_json with custom model."""
    factory = install_client(monkeypatch, text_client("{}"))

    await one_shot_json("Extract info", model="gpt-4o")

    assert factory.configs == [{"model": "gpt-4o"}]
