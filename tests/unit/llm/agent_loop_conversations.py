"""Conversations in the shapes the agent loop produces, for prefix-stability tests.

Prompt caching on every provider is a prefix match, so each provider adapter
must render a conversation's next request as an extension of its previous one.
Each adapter has a property test that generates conversations here, renders
every request the agent loop would send, and checks that property. What a
provider renders, and which differences are meant to be there, stay in that
provider's test; what replay metadata an assistant message carries is the one
provider-specific part of the conversation itself, supplied as ``decorate``.

Shapes covered: tool rounds (parallel calls, text alongside calls, a result
that activates a deferred tool), answers, new user turns, mid-conversation
system notices, and delegation wakes -- whose row is replayed on later turns
exactly as its own turn sent it, while the instruction to act is trailing
scaffolding on that turn's requests only.

A user message landing directly on tool results is left out: only an
interrupted turn produces it, since the loop otherwise answers its tool results
first. See "Deliberate simplifications" in docs/design/prompt-caching.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hypothesis import strategies as st

from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.delegation_runs import delegation_wake_record_content

if TYPE_CHECKING:
    from collections.abc import Callable

    from family_assistant.tools.types import ToolDefinition

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


STEP = st.builds(
    Step,
    kind=st.sampled_from(["tool_round", "answer", "user", "notice", "wake"]),
    calls=st.integers(min_value=1, max_value=3),
    thinking=st.booleans(),
    activates=st.booleans(),
    text=st.booleans(),
)


def opening() -> list[LLMMessage]:
    """The start of every generated conversation."""
    return [
        SystemMessage(content=SYSTEM_PROMPT, stable_prefix_len=len(SYSTEM_PROMPT)),
        UserMessage(content="Hello"),
    ]


def wake_trigger(index: int) -> str:
    """A completion wake's trigger, as the task worker writes it."""
    return (
        "System: Delegated profile task completed.\n\n"
        f"Delegation reference: delegation-{index}\n"
        "The delegated result is provided as lower-priority data in the message "
        "history for this turn. Use it to respond to the user."
    )


def sends_a_request(history: list[LLMMessage]) -> bool:
    """The agent loop calls the model whenever the model has not just spoken."""
    return not isinstance(history[-1], AssistantMessage)


def messages_for(
    step: Step,
    index: int,
    history: list[LLMMessage],
    decorate: Callable[[AssistantMessage, int, bool], AssistantMessage],
) -> list[LLMMessage]:
    """What *step* appends to *history*.

    *decorate* attaches the replay metadata a provider stores on an assistant
    message it produced, given the message, the step index and whether the
    model thought.
    """
    kind = step.kind
    answer = decorate(AssistantMessage(content=f"Answer {index}"), index, step.thinking)
    if (
        kind in {"user", "notice", "wake"}
        and history
        and isinstance(history[-1], ToolMessage)
    ):
        # The model answers its tool results before anything else arrives;
        # see the module docstring for the interrupted turn this leaves out.
        return [answer, *messages_for(step, index, [], decorate)]
    if kind == "user":
        return [UserMessage(content=f"Question {index}")]
    if kind == "notice":
        return [SystemMessage(content=f"Delegation {index} finished.")]
    if kind == "wake":
        return [
            UserMessage(content=f"Delegated result {index}"),
            SystemMessage(content=delegation_wake_record_content(wake_trigger(index))),
        ]
    if kind == "answer":
        return [answer]
    calls = [
        ToolCallItem(
            id=f"call_{index}_{n}",
            type="function",
            function=ToolCallFunction(name="activate_tools", arguments="{}"),
        )
        for n in range(step.calls)
    ]
    return [
        decorate(
            AssistantMessage(
                content=f"Checking {index}" if step.text else None, tool_calls=calls
            ),
            index,
            step.thinking,
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
