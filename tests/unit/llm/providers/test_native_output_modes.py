"""Unit tests for provider-native structured and JSON output modes."""

# pylint: disable=no-name-in-module

import json
from collections.abc import Sequence
from unittest.mock import patch

import pytest
from anthropic.types import Message, StopReason, TextBlock, ToolUseBlock, Usage
from google.genai import types
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ParsedChatCompletion,
    ParsedChatCompletionMessage,
    ParsedChoice,
)
from openai.types.chat.chat_completion import Choice
from pydantic import BaseModel, ConfigDict

from family_assistant.llm import StructuredOutputError, UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from tests.mocks.recording_async_call import RecordingAsyncCall


class SampleResponse(BaseModel):
    answer: str


class StrictResponse(BaseModel):
    """Model whose JSON schema carries the extras guard and a mapping field."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    values: dict[str, str]


def openai_completion(content: str) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model="gpt-4.1-nano",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content=content),
            )
        ],
    )


def openai_parsed_completion(
    parsed: SampleResponse, content: str
) -> ParsedChatCompletion[SampleResponse]:
    return ParsedChatCompletion[SampleResponse](
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model="gpt-4.1-nano",
        choices=[
            ParsedChoice[SampleResponse](
                index=0,
                finish_reason="stop",
                message=ParsedChatCompletionMessage[SampleResponse](
                    role="assistant", content=content, parsed=parsed
                ),
            )
        ],
    )


def anthropic_message(
    content: Sequence[TextBlock | ToolUseBlock],
    stop_reason: StopReason = "end_turn",
) -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-sonnet-4-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def gemini_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)])
            )
        ]
    )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_generate_structured_uses_native_parse() -> None:
    """OpenAI structured output should use the native parse endpoint."""
    client = OpenAIClient(api_key="test", model="gpt-4.1-nano")
    parse = RecordingAsyncCall(
        openai_parsed_completion(SampleResponse(answer="ok"), '{"answer":"ok"}')
    )

    with patch.object(client.client.beta.chat.completions, "parse", new=parse):
        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    assert parse.last_call.kwargs["response_format"] is SampleResponse


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_generate_json_uses_native_json_mode() -> None:
    """OpenAI JSON output should use response_format=json_object."""
    client = OpenAIClient(api_key="test", model="gpt-4.1-nano")
    create = RecordingAsyncCall(openai_completion('{"answer":"ok"}'))

    with patch.object(client.client.chat.completions, "create", new=create):
        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    assert create.last_call.kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_drops_the_openrouter_routing_prefix_on_the_wire() -> None:
    """`openrouter/` picks the endpoint here; OpenRouter itself knows the model
    as `vendor/model`, and rejects an id carrying our prefix."""
    client = OpenAIClient(
        api_key="test",
        model="openrouter/moonshotai/kimi-k3",
        base_url="https://openrouter.ai/api/v1",
    )
    create = RecordingAsyncCall(openai_completion('{"answer":"ok"}'))

    with patch.object(client.client.chat.completions, "create", new=create):
        await client.generate_json(messages=[UserMessage(content="Return JSON")])

    assert create.last_call.kwargs["model"] == "moonshotai/kimi-k3"
    assert client.model == "openrouter/moonshotai/kimi-k3"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_generate_structured_uses_native_json_schema() -> None:
    """Anthropic structured output is constrained by output_config.format.

    The schema goes through the SDK's transform, which drops what the API cannot
    enforce; pydantic still validates the full model on the way back.
    """
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    create = RecordingAsyncCall(
        anthropic_message([TextBlock(type="text", text='{"answer": "ok"}')])
    )

    with patch.object(client.client.messages, "create", new=create):
        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    sent = create.last_call.kwargs
    assert "tools" not in sent
    output_config = sent["output_config"]
    assert isinstance(output_config, dict)
    output_format = output_config["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["additionalProperties"] is False


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_generate_structured_retries_a_refusal() -> None:
    """A refusal is not the object asked for, so it is retried, then raised."""
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    refusal = anthropic_message([], stop_reason="refusal")
    create = RecordingAsyncCall(refusal, refusal)

    with (
        patch.object(client.client.messages, "create", new=create),
        pytest.raises(StructuredOutputError),
    ):
        await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
            max_retries=1,
        )

    assert len(create.calls) == 2


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_generate_json_uses_native_object_tool() -> None:
    """Anthropic JSON output asks for its native object tool."""
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    create = RecordingAsyncCall(
        anthropic_message(
            [
                ToolUseBlock(
                    id="toolu_test",
                    type="tool_use",
                    name="return_json_object",
                    input={"answer": "ok"},
                )
            ],
            stop_reason="tool_use",
        )
    )

    with patch.object(client.client.messages, "create", new=create):
        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    tools = create.last_call.kwargs["tools"]
    assert isinstance(tools, list)
    assert tools[0]["input_schema"]["type"] == "object"
    assert tools[0]["input_schema"]["additionalProperties"] is True


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_one_shot_outputs_write_nothing_to_the_cache() -> None:
    """A structured request is never extended by a later one, so a breakpoint
    on it would be a cache write that is never read."""
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    create = RecordingAsyncCall(
        anthropic_message([TextBlock(type="text", text='{"answer": "ok"}')]),
        anthropic_message(
            [
                ToolUseBlock(
                    id="toolu_test",
                    type="tool_use",
                    name="return_json_object",
                    input={"answer": "ok"},
                )
            ],
            stop_reason="tool_use",
        ),
    )

    with patch.object(client.client.messages, "create", new=create):
        await client.generate_structured(
            messages=[UserMessage(content="Review this tool call")],
            response_model=SampleResponse,
        )
        await client.generate_json(messages=[UserMessage(content="Return JSON")])

    assert len(create.calls) == 2
    for call in create.calls:
        assert "cache_control" not in json.dumps(call.kwargs["messages"])


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_uses_response_schema() -> None:
    """Gemini structured output should use response_schema and JSON MIME type."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    generate = RecordingAsyncCall(gemini_response('{"answer":"ok"}'))

    with patch.object(client.client.aio.models, "generate_content", new=generate):
        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    config = generate.last_call.kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SampleResponse.model_json_schema()
    assert config.response_schema is None


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_sends_strict_schema_unaltered() -> None:
    """A model Gemini's OpenAPI subset would reject must reach the API intact.

    ``extra="forbid"`` and mapping fields both emit ``additionalProperties``, which
    the ``response_schema`` proto refuses outright. The JSON Schema path accepts it,
    so the schema is passed through rather than sanitized.
    """
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    generate = RecordingAsyncCall(gemini_response('{"answer":"ok","values":{"a":"b"}}'))

    with patch.object(client.client.aio.models, "generate_content", new=generate):
        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=StrictResponse,
        )

    assert result == StrictResponse(answer="ok", values={"a": "b"})
    config = generate.last_call.kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_json_schema == StrictResponse.model_json_schema()
    assert "additionalProperties" in json.dumps(config.response_json_schema)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_retry_adds_feedback() -> None:
    """Gemini structured retries should mutate the conversation with feedback."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    generate = RecordingAsyncCall(
        gemini_response("not-json"), gemini_response('{"answer":"ok"}')
    )

    with patch.object(client.client.aio.models, "generate_content", new=generate):
        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
            max_retries=1,
        )

    assert result == SampleResponse(answer="ok")
    assert len(generate.calls) == 2
    first_contents = generate.calls[0].kwargs["contents"]
    second_contents = generate.calls[1].kwargs["contents"]
    assert isinstance(first_contents, list)
    assert isinstance(second_contents, list)
    assert len(second_contents) == len(first_contents) + 2
    assert second_contents[:-2] == first_contents
    assert second_contents[-2].role == "model"
    assert second_contents[-2].parts[0].text == "not-json"
    assert second_contents[-1].role == "user"
    feedback = second_contents[-1].parts[0].text
    assert "did not satisfy the required schema" in feedback
    assert "Error:" in feedback


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_json_uses_object_schema() -> None:
    """Gemini JSON output should use an object response schema."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    generate = RecordingAsyncCall(gemini_response('{"answer":"ok"}'))

    with patch.object(client.client.aio.models, "generate_content", new=generate):
        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    config = generate.last_call.kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_mime_type == "application/json"
    assert config.response_schema is None


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_json_retry_adds_feedback() -> None:
    """Gemini JSON retries should mutate the conversation with feedback."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    generate = RecordingAsyncCall(
        gemini_response('["wrong"]'), gemini_response('{"answer":"ok"}')
    )

    with patch.object(client.client.aio.models, "generate_content", new=generate):
        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")],
            max_retries=1,
        )

    assert result == {"answer": "ok"}
    assert len(generate.calls) == 2
    first_contents = generate.calls[0].kwargs["contents"]
    second_contents = generate.calls[1].kwargs["contents"]
    assert isinstance(first_contents, list)
    assert isinstance(second_contents, list)
    assert len(second_contents) == len(first_contents) + 2
    assert second_contents[:-2] == first_contents
    assert second_contents[-2].role == "model"
    assert second_contents[-2].parts[0].text == '["wrong"]'
    assert second_contents[-1].role == "user"
    feedback = second_contents[-1].parts[0].text
    assert "not a valid JSON object" in feedback
    assert "Error:" in feedback
