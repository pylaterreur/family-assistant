"""Unit tests for RetryingLLMClient streaming fallback."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, TypeVar
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from family_assistant.llm import (
    JsonObject,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    UserMessageDict,
)
from family_assistant.llm.base import (
    RateLimitError,
)
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.factories.messages import (
    create_user_message,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

T = TypeVar("T", bound=BaseModel)

StreamScript = Callable[[], AsyncIterator[LLMStreamEvent]]


class ScriptedStreamingClient:
    """LLM client whose streams play back scripted generators, counting calls.

    Each call to ``generate_response_stream`` consumes the next script entry;
    a call beyond the script fails, so an unexpected extra attempt is visible.
    """

    def __init__(self, streams: Sequence[StreamScript] = ()) -> None:
        self._streams = list(streams)
        self.stream_calls = 0

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        raise AssertionError("Only streaming is scripted in these tests")

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        self.stream_calls += 1
        if not self._streams:
            raise AssertionError("Unscripted call to generate_response_stream")
        return self._streams.pop(0)()

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        raise AssertionError("Only streaming is scripted in these tests")

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        raise AssertionError("Only streaming is scripted in these tests")

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        raise AssertionError("Only streaming is scripted in these tests")

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        raise AssertionError("Only streaming is scripted in these tests")


class RecordingSleep:
    """Replacement for ``asyncio.sleep`` that records delays without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def fallback_stream() -> AsyncIterator[LLMStreamEvent]:
    yield LLMStreamEvent(type="content", content="Fallback content")
    yield LLMStreamEvent(type="done", metadata={})


def rate_limited(retry_after: float | None = None) -> StreamScript:
    async def stream() -> AsyncIterator[LLMStreamEvent]:
        raise RateLimitError(
            "Rate limited", "test", "test-model", retry_after=retry_after
        )
        yield  # unreachable, makes this an async generator

    return stream


@pytest.mark.no_db
async def test_streaming_fallback_on_immediate_error() -> None:
    """Test that streaming falls back to secondary model if primary fails immediately."""
    primary = ScriptedStreamingClient([rate_limited()])
    fallback = ScriptedStreamingClient([fallback_stream])
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    events = [
        event
        async for event in client.generate_response_stream([
            create_user_message("test")
        ])
    ]

    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"
    assert primary.stream_calls == 1
    assert fallback.stream_calls == 1


@pytest.mark.no_db
async def test_streaming_no_fallback_if_content_yielded() -> None:
    """Test that streaming does NOT fall back if primary yielded content before failing."""

    async def primary_stream() -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(type="content", content="Primary content")
        raise RateLimitError("Rate limited mid-stream", "test", "test-model")

    primary = ScriptedStreamingClient([primary_stream])
    fallback = ScriptedStreamingClient()
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    with pytest.raises(RateLimitError):
        async for _event in client.generate_response_stream([
            create_user_message("test")
        ]):
            pass

    assert primary.stream_calls == 1
    assert fallback.stream_calls == 0


@pytest.mark.no_db
async def test_streaming_rate_limit_retry_with_delay() -> None:
    """Test that rate limit with retry_after <= 60s triggers a delayed retry on primary."""

    async def retry_stream() -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(type="content", content="Retry succeeded")
        yield LLMStreamEvent(type="done", metadata={})

    primary = ScriptedStreamingClient([rate_limited(retry_after=5), retry_stream])
    fallback = ScriptedStreamingClient()
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )
    sleep = RecordingSleep()

    with patch("asyncio.sleep", new=sleep):
        events = [
            event
            async for event in client.generate_response_stream([
                create_user_message("test")
            ])
        ]

    assert sleep.delays == [5]
    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Retry succeeded"
    assert primary.stream_calls == 2
    assert fallback.stream_calls == 0


@pytest.mark.no_db
async def test_streaming_rate_limit_retry_fails_then_fallback() -> None:
    """Test that if delayed retry also fails, falls through to fallback."""
    primary = ScriptedStreamingClient([
        rate_limited(retry_after=5),
        rate_limited(retry_after=5),
    ])
    fallback = ScriptedStreamingClient([fallback_stream])
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    with patch("asyncio.sleep", new=RecordingSleep()):
        events = [
            event
            async for event in client.generate_response_stream([
                create_user_message("test")
            ])
        ]

    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"
    assert primary.stream_calls == 2
    assert fallback.stream_calls == 1


@pytest.mark.no_db
async def test_streaming_rate_limit_no_retry_if_retry_after_too_long() -> None:
    """Test that rate limit with retry_after > 60s skips delay retry and goes to fallback."""
    primary = ScriptedStreamingClient([rate_limited(retry_after=120)])
    fallback = ScriptedStreamingClient([fallback_stream])
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )
    sleep = RecordingSleep()

    with patch("asyncio.sleep", new=sleep):
        events = [
            event
            async for event in client.generate_response_stream([
                create_user_message("test")
            ])
        ]

    assert sleep.delays == []
    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"
    assert primary.stream_calls == 1
    assert fallback.stream_calls == 1


@pytest.mark.no_db
async def test_anthropic_primary_failing_before_output_streams_from_fallback() -> None:
    """A real Anthropic primary that fails before any output hands off.

    The thinking budget here cannot fit under max_tokens, so the request fails
    locally before a byte is streamed -- the same point an outage fails at.
    """
    fallback = ScriptedStreamingClient([fallback_stream])
    primary = AnthropicClient(
        api_key="test-key",
        model="claude-sonnet-4-6",
        model_parameters={
            "claude-sonnet-4-6": {
                "thinking": {"type": "enabled", "budget_tokens": 64_000}
            }
        },
    )
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="claude-sonnet-4-6",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    events = [
        event
        async for event in client.generate_response_stream([
            create_user_message("test")
        ])
    ]

    assert [event.type for event in events] == ["content", "done"]
    assert events[0].content == "Fallback content"
    assert fallback.stream_calls == 1
