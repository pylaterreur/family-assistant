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

A delegation wake is one of those shapes: its row is replayed on later turns
exactly as its own turn sent it, and what tells the wake turn to act is
trailing scaffolding, sent after the cache breakpoint on each of that turn's
requests and on no later one. The earlier request is compared without it.

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
from family_assistant.storage.delegation_runs import (
    delegation_wake_instruction,
    delegation_wake_record_content,
)

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

StepKind = Literal["tool_round", "answer", "user", "notice", "wake"]


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
    kind=st.sampled_from(["tool_round", "answer", "user", "notice", "wake"]),
    calls=st.integers(min_value=1, max_value=3),
    thinking=st.booleans(),
    activates=st.booleans(),
    text=st.booleans(),
)

WireMessage = dict[str, object]


def _wake_trigger(index: int) -> str:
    """A completion wake's trigger, as the task worker writes it."""
    return (
        "System: Delegated profile task completed.\n\n"
        f"Delegation reference: delegation-{index}\n"
        "The delegated result is provided as lower-priority data in the message "
        "history for this turn. Use it to respond to the user."
    )


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
    if (
        kind in {"user", "notice", "wake"}
        and history
        and isinstance(history[-1], ToolMessage)
    ):
        # The model answers its tool results before anything else arrives;
        # see the module docstring for the interrupted turn this leaves out.
        return [_answer(index, thinking=step.thinking), *_messages_for(step, index, [])]
    if kind == "user":
        return [UserMessage(content=f"Question {index}")]
    if kind == "notice":
        return [SystemMessage(content=f"Delegation {index} finished.")]
    if kind == "wake":
        return [
            UserMessage(content=f"Delegated result {index}"),
            SystemMessage(content=delegation_wake_record_content(_wake_trigger(index))),
        ]
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


def _request(
    client: AnthropicClient, history: list[LLMMessage], instruction: str | None
) -> WireMessage:
    """The request for *history*, with the turn's trailing instruction if any."""
    trailing = (
        [UserMessage(content=instruction, is_turn_scaffolding=True)]
        if instruction is not None
        else []
    )
    return client._assemble_request(
        [*history, *trailing], TOOLS, "auto", streaming=True
    )


def _breakpoint_index(api_messages: list[WireMessage]) -> int:
    """Where the conversation breakpoint sits, as a flattened block index."""
    index = 0
    found: int | None = None
    for message in api_messages:
        content = message["content"]
        blocks = (
            [content] if isinstance(content, str) else cast("list[object]", content)
        )
        for block in blocks:
            if isinstance(block, dict) and "cache_control" in block:
                found = index
            index += 1
    assert found is not None
    return found


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
        instruction: str | None = None
        previous = _request(client, history, instruction)
        previous_instruction = instruction

        for index, step in enumerate(steps):
            history.extend(_messages_for(step, index, history))
            if step.kind == "wake":
                instruction = delegation_wake_instruction(_wake_trigger(index))
            elif step.kind == "user":
                instruction = None
            if not _sends_a_request(history):
                continue
            current = _request(client, history, instruction)

            assert current["tools"] == previous["tools"]
            assert _strip(current["system"]) == _strip(previous["system"])
            previous_messages = cast("list[WireMessage]", previous["messages"])
            earlier = _flattened(previous_messages)
            instruction_block = (
                ("user", {"type": "text", "text": previous_instruction})
                if previous_instruction is not None
                else None
            )
            # Only the trailing instruction and tool additions, which carry no
            # breakpoint, come after it: dropping the instruction loses nothing
            # that was cached.
            after_breakpoint = earlier[_breakpoint_index(previous_messages) + 1 :]
            assert all(
                entry == instruction_block
                or cast("dict[str, object]", entry[1])["type"] == "tool_addition"
                for entry in after_breakpoint
            )
            if instruction_block is not None:
                assert instruction_block in after_breakpoint
                # A tool addition can follow it, so it is not always last.
                earlier.remove(instruction_block)
            later = _flattened(cast("list[WireMessage]", current["messages"]))
            assert later[: len(earlier)] == earlier
            previous = current
            previous_instruction = instruction
