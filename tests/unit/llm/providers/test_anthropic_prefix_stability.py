"""The Anthropic request for a conversation is a prefix of the next one's.

Prompt caching is a prefix match over ``tools``, then ``system``, then
``messages``: a byte that changes anywhere in what an earlier request sent
throws away the cache from that point on, and with it every thinking block
after it. Nothing reports that as an error -- the request succeeds, at full
price -- so the client's conversion has to hold the property by construction.

These tests generate conversations in the shapes the agent loop produces (tool
rounds, answers, new user turns, mid-conversation system notices, replayed
thinking, deferred tools activated by a result) and check that every request
the loop would send extends the one before it. Cache breakpoints are stripped
first, since the moving marker is the one difference that is meant to be there.
The diagnostics opt-in is a request parameter rather than part of the prompt,
so it is not compared.

One extension is left out on purpose: a user message landing directly on tool
results, which only an interrupted turn produces (the loop otherwise sends the
results and appends the model's reply). A deferred tool those results activated
is surfaced by a ``tool_addition`` system message, which the API accepts only
before an assistant turn or at the end, so the interruption moves it past the
new message and the prefix breaks there. That costs one cache rewrite on a rare
path; see "Deliberate simplifications" in docs/design/prompt-caching.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem

if TYPE_CHECKING:
    from family_assistant.llm import JsonObject
    from family_assistant.tools.types import ToolDefinition

# A model that takes deferred tools natively, so activations go through
# tool_addition messages rather than changing the tool list.
MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = "You are the family's household assistant."

TOOLS: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "activate_tools",
            "description": "Load on-demand tools by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_names": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["tool_names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_lights",
            "description": "Turn the lights in a room on or off.",
            "parameters": {
                "type": "object",
                "properties": {"room": {"type": "string"}},
                "required": ["room"],
            },
        },
        "defer_loading": True,
    },
]

StepKind = Literal["tool_round", "answer", "user", "notice"]


@dataclass(frozen=True)
class Step:
    """One thing the agent loop appends to a conversation."""

    kind: StepKind
    calls: int
    thinking: bool
    activates: bool
    text: bool


_STEP = st.builds(
    Step,
    kind=st.sampled_from(["tool_round", "answer", "user", "notice"]),
    calls=st.integers(min_value=1, max_value=3),
    thinking=st.booleans(),
    activates=st.booleans(),
    text=st.booleans(),
)

WireMessage = dict[str, object]


def _metadata(index: int, *, thinking: bool) -> JsonObject:
    """Provider metadata as the client stores it for a streamed turn."""
    return {
        "provider": "anthropic",
        "response_id": f"msg_{index}",
        "thinking_blocks": (
            [{"type": "thinking", "thinking": f"plan {index}", "signature": "sig"}]
            if thinking
            else []
        ),
    }


def _answer(index: int, *, thinking: bool) -> AssistantMessage:
    return AssistantMessage(
        content=f"Answer {index}",
        provider_metadata=_metadata(index, thinking=thinking),
    )


def _messages_for(
    step: Step, index: int, history: list[LLMMessage]
) -> list[LLMMessage]:
    kind = step.kind
    if kind in {"user", "notice"} and history and isinstance(history[-1], ToolMessage):
        # The model answers its tool results before anything else arrives;
        # see the module docstring for the interrupted turn this leaves out.
        return [_answer(index, thinking=step.thinking), *_messages_for(step, index, [])]
    if kind == "user":
        return [UserMessage(content=f"Question {index}")]
    if kind == "notice":
        return [SystemMessage(content=f"Delegation {index} finished.")]
    if kind == "answer":
        return [_answer(index, thinking=step.thinking)]
    calls = [
        ToolCallItem(
            id=f"call_{index}_{n}",
            type="function",
            function=ToolCallFunction(name="activate_tools", arguments="{}"),
        )
        for n in range(step.calls)
    ]
    return [
        AssistantMessage(
            content=f"Checking {index}" if step.text else None,
            tool_calls=calls,
            provider_metadata=_metadata(index, thinking=step.thinking),
        ),
        *(
            ToolMessage(
                tool_call_id=call.id,
                name="activate_tools",
                content=f"result {call.id}",
                activated_tools=(["set_lights"] if step.activates and n == 0 else None),
            )
            for n, call in enumerate(calls)
        ),
    ]


def _strip(value: object) -> object:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def _flattened(api_messages: list[WireMessage]) -> list[tuple[str, object]]:
    """Each content block with the role it is sent under.

    Block-level, because two same-role messages are merged into one: a request
    ending in tool results is extended by a user message joining that turn,
    which leaves the earlier blocks where they were.
    """
    flat: list[tuple[str, object]] = []
    for message in api_messages:
        content = message["content"]
        blocks = (
            [{"type": "text", "text": content}]
            if isinstance(content, str)
            else cast("list[object]", content)
        )
        flat.extend((str(message["role"]), _strip(block)) for block in blocks)
    return flat


def _request(client: AnthropicClient, history: list[LLMMessage]) -> WireMessage:
    return client._assemble_request(history, TOOLS, "auto", streaming=True)


def _sends_a_request(history: list[LLMMessage]) -> bool:
    """The agent loop calls the model whenever the model has not just spoken."""
    return not isinstance(history[-1], AssistantMessage)


@pytest.mark.no_db
class TestAnthropicPrefixStability:
    @given(st.lists(_STEP, min_size=1, max_size=12))
    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_each_request_extends_the_previous_one(self, steps: list[Step]) -> None:
        client = AnthropicClient(api_key="test", model=MODEL)
        history: list[LLMMessage] = [
            SystemMessage(content=SYSTEM_PROMPT, stable_prefix_len=len(SYSTEM_PROMPT)),
            UserMessage(content="Hello"),
        ]
        previous = _request(client, history)

        for index, step in enumerate(steps):
            history.extend(_messages_for(step, index, history))
            if not _sends_a_request(history):
                continue
            current = _request(client, history)

            assert current["tools"] == previous["tools"]
            assert _strip(current["system"]) == _strip(previous["system"])
            earlier = _flattened(cast("list[WireMessage]", previous["messages"]))
            later = _flattened(cast("list[WireMessage]", current["messages"]))
            assert later[: len(earlier)] == earlier
            previous = current
