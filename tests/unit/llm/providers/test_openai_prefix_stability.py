"""The OpenAI Responses request for a conversation is a prefix of the next one's.

OpenAI's prompt cache is a prefix match over the tools and the input items, so
a byte that changes anywhere in what an earlier request sent throws the cache
away from that point on. Nothing reports it -- the request succeeds at full
price -- so the client's conversion has to hold the property by construction.

This checks every request the agent loop would send against the one before it,
on a model that takes cache controls: the ``tools`` array never changes (an
activation arrives as an ``additional_tools`` item instead), the cache options
never change, and the input only grows. The one item allowed to drop out is a
delegation wake's trailing instruction, which is sent on that turn's requests
and on no later one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from family_assistant.llm.messages import AssistantMessage, LLMMessage, UserMessage
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.storage.delegation_runs import delegation_wake_instruction
from tests.unit.llm.agent_loop_conversations import (
    STEP,
    TOOLS,
    Step,
    messages_for,
    opening,
    sends_a_request,
    wake_trigger,
)

if TYPE_CHECKING:
    from family_assistant.llm import JsonObject

MODEL = "gpt-6-sol"


@pytest.fixture(autouse=True)
def _clear_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def _decorate(
    message: AssistantMessage, index: int, thinking: bool
) -> AssistantMessage:
    """Attach the Responses output a streamed turn stores, which is replayed verbatim."""
    output: list[JsonObject] = []
    if thinking:
        output.append({
            "type": "reasoning",
            "id": f"rs_{index}",
            "summary": [],
            "encrypted_content": f"opaque-{index}",
            "status": "completed",
        })
    if message.content:
        output.append({
            "type": "message",
            "id": f"msg_{index}",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": message.content, "annotations": []}
            ],
        })
    for call in message.tool_calls or ():
        arguments = call.function.arguments
        output.append({
            "type": "function_call",
            "id": f"fc_{call.id}",
            "call_id": call.id,
            "name": call.function.name,
            # The API returns arguments as a JSON string.
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(arguments),
            "status": "completed",
        })
    return message.model_copy(
        update={
            "provider_metadata": {
                "openai_response_output": output,
                "openai_response_stored": False,
            }
        }
    )


def _request(
    client: OpenAIClient, history: list[LLMMessage], instruction: str | None
) -> dict[str, object]:
    """The request for *history*, with the turn's trailing instruction if any."""
    trailing = (
        [UserMessage(content=instruction, is_turn_scaffolding=True)]
        if instruction is not None
        else []
    )
    return client._build_responses_params(
        [*history, *trailing], TOOLS, "auto", stream=True
    )


def _input(params: dict[str, object]) -> list[object]:
    items = params["input"]
    assert isinstance(items, list)
    return items


def _cache_options(params: dict[str, object]) -> object:
    extra = params.get("extra_body")
    return extra.get("prompt_cache_options") if isinstance(extra, dict) else None


@pytest.mark.no_db
class TestOpenAIPrefixStability:
    @given(st.lists(STEP, min_size=1, max_size=12))
    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_each_request_extends_the_previous_one(self, steps: list[Step]) -> None:
        client = OpenAIClient(api_key="test", model=MODEL)
        assert client._supports_cache_controls()
        history = opening()
        instruction: str | None = None
        previous = _request(client, history, instruction)
        previous_instruction = instruction

        for index, step in enumerate(steps):
            history.extend(messages_for(step, index, history, _decorate))
            if step.kind == "wake":
                instruction = delegation_wake_instruction(wake_trigger(index))
            elif step.kind == "user":
                instruction = None
            if not sends_a_request(history):
                continue
            current = _request(client, history, instruction)

            assert current.get("tools") == previous.get("tools")
            assert _cache_options(current) == _cache_options(previous)
            earlier = _input(previous)
            if previous_instruction is not None:
                # Always last: an activation's `additional_tools` item goes
                # before the trailing instruction, after the outputs.
                assert earlier[-1] == {"role": "user", "content": previous_instruction}
                earlier = earlier[:-1]
            later = _input(current)
            assert later[: len(earlier)] == earlier
            previous = current
            previous_instruction = instruction
