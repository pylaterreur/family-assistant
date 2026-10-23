"""OpenAI cache controls, checked against what the Responses API reports.

The unit tests pin the request shape. This checks that the API accepts it on
the agent loop's real requests -- a 12-hour breakpoint on the stable system
prompt, the implicit breakpoint kept for the loop, a fixed ``tools`` array with
an activated tool surfaced by an ``additional_tools`` item, and explicit-only
mode on a one-shot request -- and that each does what it is for, in the API's
own account of the cache.

VCR matching ignores cache directives, so replay cannot notice a breakpoint
moving; what the cassette holds is the usage the API reported for the
recorded requests.
"""

import os
from pathlib import Path

import pytest

from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.tools.types import ToolDefinition

from .vcr_helpers import sanitize_response

# The deep-tier experiment arm.
MODEL = "gpt-6.1-sol"

# Above the 1,024-token minimum cacheable prefix, so a missing read means the
# breakpoint was refused rather than that the prefix was short.
MIN_CACHEABLE_TOKENS = 1024
STABLE_SYSTEM_PROMPT = "You are the family's household assistant.\n\n" + "\n".join(
    f"House rule {i}: when asked about room {i}, refer to it as room number {i} "
    f"and remember that its lights are on circuit {i % 7}."
    for i in range(150)
)

TOOLS: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "activate_tools",
            "description": "Load on-demand tools by name so they can be called.",
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
                "properties": {
                    "room": {"type": "string"},
                    "on": {"type": "boolean"},
                },
                "required": ["room", "on"],
            },
        },
        "defer_loading": True,
    },
]


@pytest.fixture
def require_cassette(llm_record_mode: str, request: pytest.FixtureRequest) -> None:
    """Name the missing recording instead of failing on a downstream assertion."""
    cassette = Path("tests/cassettes/llm") / f"{request.node.name}.yaml"
    if llm_record_mode == "replay" and not cassette.exists():
        pytest.fail(
            f"Cassette missing at {cassette}. Record with LLM_RECORD_MODE=record."
        )


@pytest.fixture(autouse=True)
def _clear_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("require_cassette")
async def test_cache_controls_on_the_agent_loop_and_a_one_shot(
    llm_record_mode: str,
) -> None:
    """A tool round that loads a tool, its continuation, and a one-shot call.

    The continuation must read back past the system prompt into the
    conversation -- which it can only do if loading the tool left the ``tools``
    array alone -- and must be able to call the tool the ``additional_tools``
    item surfaced. The one-shot call shares only the tools and system prompt:
    it reads them from the 12-hour entry and, in explicit-only mode, writes
    nothing past them.

    Recorded cold. Re-recording within the cache lifetime finds entries warm,
    where they are read rather than written; change the stable prompt to record
    cold again.
    """
    if llm_record_mode != "replay" and not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Recording this test requires OPENAI_API_KEY")

    client = OpenAIClient(
        api_key=os.getenv("OPENAI_API_KEY", "test-openai-key"),
        model=MODEL,
        model_parameters={MODEL: {"reasoning_effort": "low"}},
    )
    assert client._supports_cache_controls()

    system = SystemMessage(
        content=STABLE_SYSTEM_PROMPT, stable_prefix_len=len(STABLE_SYSTEM_PROMPT)
    )
    first_turn: list[LLMMessage] = [
        system,
        UserMessage(
            content=(
                "Turn on the kitchen lights. The set_lights tool is not loaded "
                "yet, so load it with activate_tools first."
            )
        ),
    ]

    events = [
        event
        async for event in client.generate_response_stream(first_turn, tools=TOOLS)
    ]
    calls = [e.tool_call for e in events if e.type == "tool_call" and e.tool_call]
    assert [call.function.name for call in calls] == ["activate_tools"]
    first_done = next(e for e in events if e.type == "done")
    assert first_done.metadata is not None
    first = first_done.metadata.get("reasoning_info")
    assert first is not None

    continuation: list[LLMMessage] = [
        *first_turn,
        AssistantMessage(
            content=None,
            tool_calls=calls,
            provider_metadata=first_done.metadata.get("provider_metadata"),
        ),
        ToolMessage(
            tool_call_id=calls[0].id,
            name="activate_tools",
            content="Activated tools: set_lights.",
            activated_tools=["set_lights"],
        ),
    ]
    events = [
        event
        async for event in client.generate_response_stream(continuation, tools=TOOLS)
    ]
    loaded_calls = [
        e.tool_call for e in events if e.type == "tool_call" and e.tool_call
    ]
    # The tool reached the model through `additional_tools` alone.
    assert [call.function.name for call in loaded_calls] == ["set_lights"]
    second_done = next(e for e in events if e.type == "done")
    assert second_done.metadata is not None
    second = second_done.metadata.get("reasoning_info")
    assert second is not None

    one_shot_output = await client.generate_response(
        [system, UserMessage(content="Which circuit is room 12 on? One line.")],
        tools=TOOLS,
    )
    await client.close()
    one_shot = one_shot_output.reasoning_info
    assert one_shot is not None

    # The one-shot call shares only the tools and system prompt, so what it
    # reads is that prefix -- from the 12-hour entry the first call wrote --
    # and in explicit-only mode it writes nothing past it.
    prefix_tokens = one_shot.get("cached_prompt_tokens") or 0
    assert prefix_tokens > MIN_CACHEABLE_TOKENS
    assert not one_shot.get("cache_write_tokens")

    # The continuation read past that prefix into the conversation the first
    # call sent: loading a tool did not change anything ahead of it.
    assert (second.get("cached_prompt_tokens") or 0) > prefix_tokens
    assert (second.get("cached_prompt_tokens") or 0) >= (
        first.get("prompt_tokens") or 0
    ) - MIN_CACHEABLE_TOKENS
