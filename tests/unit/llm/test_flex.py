"""Background calls go at Flex and degrade to Standard on the same model."""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

import pytest
from pydantic import BaseModel

from family_assistant.llm import JsonObject, LLMOutput, LLMStreamEvent, UserMessageDict
from family_assistant.llm.base import InvalidRequestError, RateLimitError
from family_assistant.llm.call_context import (
    ServiceTier,
    Workload,
    current_service_tier,
    running_as,
)
from family_assistant.llm.flex import FlexClient
from family_assistant.llm.messages import LLMMessage, UserMessage
from family_assistant.tools.types import ToolAttachment, ToolDefinition

type Behaviour = Callable[[ServiceTier], LLMOutput]


def _answer(tier: ServiceTier) -> LLMOutput:
    return LLMOutput(content=f"answered at {tier}")


def _rate_limited_at_flex(tier: ServiceTier) -> LLMOutput:
    if tier == "flex":
        raise RateLimitError("no flex capacity", provider="google", model="m")
    return _answer(tier)


class TierRecordingClient:
    """A fake provider client that reports the tier each request was sent at.

    The tier is read where a real client reads it: inside the call, while the
    request is being built.
    """

    def __init__(
        self,
        behaviour: Behaviour = _answer,
        *,
        stream_events: Callable[[ServiceTier], list[LLMStreamEvent]] | None = None,
        hold_flex: asyncio.Event | None = None,
    ) -> None:
        self.model = "gemini-3.8-flash"
        self.tiers: list[ServiceTier] = []
        self._behaviour = behaviour
        self._stream_events = stream_events
        self._hold_flex = hold_flex
        self.closed = False

    async def _call(self) -> LLMOutput:
        tier = current_service_tier()
        self.tiers.append(tier)
        if tier == "flex" and self._hold_flex is not None:
            await self._hold_flex.wait()
        return self._behaviour(tier)

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        return await self._call()

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        tier = current_service_tier()
        self.tiers.append(tier)
        if tier == "flex" and self._hold_flex is not None:
            await self._hold_flex.wait()
        if self._stream_events is not None:
            for event in self._stream_events(tier):
                yield event
            return
        output = self._behaviour(tier)
        yield LLMStreamEvent(type="content", content=output.content)
        # Read again mid-stream: an accepted Flex stream stays at Flex.
        yield LLMStreamEvent(type="content", content=current_service_tier())
        yield LLMStreamEvent(type="done")

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        output = await self._call()
        return response_model.model_validate({"answer": output.content})

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        output = await self._call()
        return {"answer": output.content}

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        return {"role": "user", "content": prompt_text or ""}

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


MESSAGES: list[LLMMessage] = [UserMessage(content="hello")]


def _flex(inner: TierRecordingClient, clock: FakeClock | None = None) -> FlexClient:
    return FlexClient(
        inner,
        model=inner.model,
        max_wait_seconds=0.05,
        cooldown_seconds=600,
        clock=clock or FakeClock(),
    )


async def _collect(stream: AsyncIterator[LLMStreamEvent]) -> list[LLMStreamEvent]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_an_interactive_call_is_sent_at_standard() -> None:
    inner = TierRecordingClient()

    output = await _flex(inner).generate_response(MESSAGES)

    assert output.content == "answered at standard"
    assert inner.tiers == ["standard"]


@pytest.mark.asyncio
async def test_a_background_call_is_sent_at_flex() -> None:
    inner = TierRecordingClient()

    with running_as(Workload.BACKGROUND):
        output = await _flex(inner).generate_response(MESSAGES)

    assert output.content == "answered at flex"
    assert inner.tiers == ["flex"]


@pytest.mark.asyncio
async def test_no_flex_capacity_retries_at_standard_on_the_same_client() -> None:
    inner = TierRecordingClient(_rate_limited_at_flex)

    with running_as(Workload.BACKGROUND):
        output = await _flex(inner).generate_response(MESSAGES)

    assert output.content == "answered at standard"
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_a_flex_attempt_past_the_maximum_wait_retries_at_standard() -> None:
    inner = TierRecordingClient(hold_flex=asyncio.Event())

    with running_as(Workload.BACKGROUND):
        output = await _flex(inner).generate_response(MESSAGES)

    assert output.content == "answered at standard"
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_a_capacity_failure_pauses_flex_for_the_cooldown() -> None:
    inner = TierRecordingClient(_rate_limited_at_flex)
    clock = FakeClock()
    client = _flex(inner, clock)

    with running_as(Workload.BACKGROUND):
        await client.generate_response(MESSAGES)
        inner.tiers.clear()
        clock.now += 599
        await client.generate_response(MESSAGES)

    assert inner.tiers == ["standard"]


@pytest.mark.asyncio
async def test_flex_resumes_once_the_cooldown_has_passed() -> None:
    inner = TierRecordingClient(_rate_limited_at_flex)
    clock = FakeClock()
    client = _flex(inner, clock)

    with running_as(Workload.BACKGROUND):
        await client.generate_response(MESSAGES)
        inner.tiers.clear()
        clock.now += 601
        await client.generate_response(MESSAGES)

    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_a_failure_that_is_not_about_capacity_is_not_retried() -> None:
    def invalid(tier: ServiceTier) -> LLMOutput:
        raise InvalidRequestError("bad request", provider="google", model="m")

    inner = TierRecordingClient(invalid)

    with running_as(Workload.BACKGROUND), pytest.raises(InvalidRequestError):
        await _flex(inner).generate_response(MESSAGES)

    assert inner.tiers == ["flex"]


@pytest.mark.asyncio
async def test_a_standard_failure_after_degrading_propagates_to_the_chain() -> None:
    def always_limited(tier: ServiceTier) -> LLMOutput:
        raise RateLimitError("no capacity", provider="google", model="m")

    inner = TierRecordingClient(always_limited)

    with running_as(Workload.BACKGROUND), pytest.raises(RateLimitError):
        await _flex(inner).generate_response(MESSAGES)

    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_an_accepted_flex_stream_stays_at_flex_to_the_end() -> None:
    inner = TierRecordingClient()

    with running_as(Workload.BACKGROUND):
        events = await _collect(_flex(inner).generate_response_stream(MESSAGES))

    assert [event.content for event in events] == ["answered at flex", "flex", None]


@pytest.mark.asyncio
async def test_the_tier_does_not_leak_into_the_stream_consumer() -> None:
    inner = TierRecordingClient()
    seen_by_consumer: list[ServiceTier] = []

    with running_as(Workload.BACKGROUND):
        async for _event in _flex(inner).generate_response_stream(MESSAGES):
            seen_by_consumer.append(current_service_tier())

    assert seen_by_consumer == ["standard", "standard", "standard"]


@pytest.mark.asyncio
async def test_a_stream_that_raises_before_any_event_degrades_to_standard() -> None:
    inner = TierRecordingClient(_rate_limited_at_flex)

    with running_as(Workload.BACKGROUND):
        events = await _collect(_flex(inner).generate_response_stream(MESSAGES))

    assert events[0].content == "answered at standard"
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_a_rate_limit_error_event_first_degrades_to_standard() -> None:
    def events_for(tier: ServiceTier) -> list[LLMStreamEvent]:
        if tier == "flex":
            return [
                LLMStreamEvent(
                    type="error",
                    error="429 resource exhausted",
                    metadata={"error_type": "rate_limit", "provider": "openai"},
                )
            ]
        return [LLMStreamEvent(type="content", content="standard reply")]

    inner = TierRecordingClient(stream_events=events_for)

    with running_as(Workload.BACKGROUND):
        events = await _collect(_flex(inner).generate_response_stream(MESSAGES))

    assert [event.content for event in events] == ["standard reply"]
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_a_stream_queued_past_the_maximum_wait_degrades_to_standard() -> None:
    inner = TierRecordingClient(hold_flex=asyncio.Event())

    with running_as(Workload.BACKGROUND):
        events = await _collect(_flex(inner).generate_response_stream(MESSAGES))

    assert events[0].content == "answered at standard"
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_an_error_after_the_stream_started_is_not_retried() -> None:
    def events_for(tier: ServiceTier) -> list[LLMStreamEvent]:
        return [
            LLMStreamEvent(type="content", content="partial"),
            LLMStreamEvent(
                type="error",
                error="429 resource exhausted",
                metadata={"error_type": "rate_limit", "provider": "openai"},
            ),
        ]

    inner = TierRecordingClient(stream_events=events_for)

    with running_as(Workload.BACKGROUND):
        events = await _collect(_flex(inner).generate_response_stream(MESSAGES))

    assert [event.type for event in events] == ["content", "error"]
    assert inner.tiers == ["flex"]


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_structured_output_degrades_like_a_plain_call() -> None:
    inner = TierRecordingClient(_rate_limited_at_flex)

    with running_as(Workload.BACKGROUND):
        result = await _flex(inner).generate_structured(MESSAGES, Answer)

    assert result == Answer(answer="answered at standard")
    assert inner.tiers == ["flex", "standard"]


@pytest.mark.asyncio
async def test_json_output_is_sent_at_flex_in_the_background() -> None:
    inner = TierRecordingClient()

    with running_as(Workload.BACKGROUND):
        result = await _flex(inner).generate_json(MESSAGES)

    assert result == {"answer": "answered at flex"}


def test_attributes_it_does_not_define_come_from_the_wrapped_client() -> None:
    inner = TierRecordingClient()

    assert _flex(inner).model == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_closing_the_wrapper_closes_the_wrapped_client() -> None:
    inner = TierRecordingClient()

    await _flex(inner).close()

    assert inner.closed
