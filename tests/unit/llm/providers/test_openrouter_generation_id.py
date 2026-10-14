"""OpenRouter's generation id survives the call, finished or not.

OpenRouter bills and logs every call under a ``gen-...`` id, sent on the first
chunk of a stream, while its usage arrives only on the last. These go through
the real OpenAI SDK to an in-process transport serving an OpenRouter-shaped
stream, so the id is read from where the SDK actually puts it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from openai import AsyncOpenAI

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.request_buffer import LLMRequestBuffer, get_request_buffer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from family_assistant.llm import LLMStreamEvent

_BASE_URL = "https://openrouter.ai/api/v1"
_GENERATION_ID = "gen-1760400000-abcdefghijklmnop"


def _chunk(delta: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "id": _GENERATION_ID,
        "object": "chat.completion.chunk",
        "created": 1760400000,
        "model": "moonshotai/kimi-k3",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        **extra,
    }


_CHUNKS = [
    _chunk({"role": "assistant", "content": "Hel"}),
    _chunk({"content": "lo"}),
    {
        **_chunk({}),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
    {
        **_chunk({}),
        "choices": [],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
    },
]


def _sse_handler(_request: httpx.Request) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in _CHUNKS)
    return httpx.Response(
        200,
        content=(body + "data: [DONE]\n\n").encode(),
        headers={"content-type": "text/event-stream"},
    )


@pytest.fixture
def buffer() -> Iterator[LLMRequestBuffer]:
    """Empties the global diagnostics buffer around each test."""
    request_buffer = get_request_buffer()
    request_buffer.clear()
    yield request_buffer
    request_buffer.clear()


def _stream() -> AsyncGenerator[LLMStreamEvent]:
    client = OpenAIClient(
        api_key="test-key", model="openrouter/moonshotai/kimi-k3", base_url=_BASE_URL
    )
    client.client = AsyncOpenAI(
        api_key="test-key",
        base_url=_BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_sse_handler)),
    )
    return cast(
        "AsyncGenerator[LLMStreamEvent]",
        client.generate_response_stream([UserMessage(content="hi")]),
    )


async def test_a_finished_stream_persists_the_generation_id(
    buffer: LLMRequestBuffer,
) -> None:
    """The ``done`` event's usage is the copy written to the message row."""
    del buffer
    done = [event async for event in _stream() if event.type == "done"]

    assert len(done) == 1
    assert done[0].metadata is not None
    usage = done[0].metadata.get("reasoning_info")
    assert usage is not None
    assert usage.get("response_id") == _GENERATION_ID
    assert usage.get("prompt_tokens") == 12


async def test_an_abandoned_stream_leaves_a_record_with_the_generation_id(
    buffer: LLMRequestBuffer,
) -> None:
    """A task timeout cancels the turn before the usage chunk arrives.

    No message row is written for that turn, so the diagnostics record is
    where the generation id has to be found.
    """
    stream = _stream()
    async for event in stream:
        if event.type == "content":
            break
    await stream.aclose()

    records = buffer.get_recent()
    assert len(records) == 1
    assert records[0].response_id == _GENERATION_ID
    assert records[0].error == "cancelled before completion"
