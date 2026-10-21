"""Test the Google Antigravity managed agent path through the Interactions API."""

import importlib
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from google import genai
from google.genai.interactions import (
    Interaction,
    InteractionCompletedEvent,
    InteractionCreatedEvent,
    InteractionSSEEvent,
    InteractionSseEventInteraction,
    StepDelta,
    TextDelta,
)
from pydantic import TypeAdapter

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.antigravity_egress import (
    GITHUB_GIT_AUTH_ENV,
    AntigravityEgressError,
    EgressNetworkPayload,
    EgressResolution,
)
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    UserMessage,
)
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    SandboxFile,
    is_antigravity_model,
    is_deep_research_model,
    is_interactions_agent_model,
)
from family_assistant.processing.protocol import (
    DelegationPermanentError,
    DelegationTransientError,
)

ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"

_SDK_INTERACTIONS_MODULE = importlib.import_module("google.genai._gaos.models")
_SDK_COMPAT_ERRORS = importlib.import_module("google.genai._gaos.lib.compat_errors")
_SDK_ENVIRONMENT_TYPES = importlib.import_module(
    "google.genai._gaos.types.environments.environment"
)
_SDK_ASYNC_INTERACTIONS = type(genai.Client(api_key="test").aio.interactions)
# The client-facing environments resource takes *args/**kwargs and forwards
# to this base, which declares the real keywords, so the base is what gets
# specced.
_SDK_ASYNC_ENVIRONMENTS_BASE = importlib.import_module(
    "google.genai._gaos.environments"
).AsyncEnvironments
_SDK_ASYNC_ENVIRONMENT_FILES = type(genai.Client(api_key="test").aio.environments.files)
_CREATE_INTERACTION_ADAPTER = TypeAdapter(
    _SDK_INTERACTIONS_MODULE.CreateInteractionRequestBody
)


@dataclass(frozen=True)
class _SdkCalls:
    """The installed SDK's network-facing async methods, autospecced.

    Each is patched on its class, so a call records the resource instance as
    its first argument, and a method this SDK version lacks or a keyword it
    does not take fails here rather than in production.
    """

    create: AsyncMock
    get: AsyncMock
    create_environment: AsyncMock
    upload: AsyncMock


@pytest.fixture
def sdk() -> Generator[_SdkCalls]:
    with (
        patch.object(_SDK_ASYNC_INTERACTIONS, "create", autospec=True) as create,
        patch.object(_SDK_ASYNC_INTERACTIONS, "get", autospec=True) as get,
        patch.object(
            _SDK_ASYNC_ENVIRONMENTS_BASE, "create_environment", autospec=True
        ) as create_environment,
        patch.object(_SDK_ASYNC_ENVIRONMENT_FILES, "upload", autospec=True) as upload,
    ):
        create_environment.return_value = _SDK_ENVIRONMENT_TYPES.Environment(id="env_1")
        yield _SdkCalls(
            create=create,
            get=get,
            create_environment=create_environment,
            upload=upload,
        )


async def _drain_stream(
    client: GoogleGenAIClient, messages: list[LLMMessage]
) -> list[LLMStreamEvent]:
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)
    return events


def _completing_stream(
    interaction_id: str, text: str
) -> AsyncGenerator[InteractionSSEEvent]:
    async def generator() -> AsyncGenerator[InteractionSSEEvent]:
        yield InteractionCreatedEvent(
            interaction=InteractionSseEventInteraction(
                id=interaction_id, status="in_progress"
            ),
            event_id="evt_0",
        )
        yield StepDelta(delta=TextDelta(text=text), index=0, event_id="evt_1")
        yield InteractionCompletedEvent(
            interaction=InteractionSseEventInteraction(
                id=interaction_id, status="completed"
            ),
            event_id="evt_2",
        )

    return generator()


def _accept_submissions(sdk: _SdkCalls) -> AsyncMock:
    """Make the SDK's create() accept a submit-path request, and return it."""
    sdk.create.return_value = Interaction(id="inter_ag_submitted", status="in_progress")
    return sdk.create


@pytest.mark.parametrize(
    "model_id",
    [
        ANTIGRAVITY_AGENT_ID,
        "antigravity-preview-09-2027",
        # The client prefixes every configured model id with `models/`, so the
        # predicate has to match its own `model_name` too -- otherwise the
        # profile falls through to an ordinary chat completion.
        f"models/{ANTIGRAVITY_AGENT_ID}",
    ],
)
def test_antigravity_model_ids_route_to_the_agent_path(model_id: str) -> None:
    """The current preview id and any later antigravity revision both match."""
    assert is_antigravity_model(model_id) is True
    assert is_interactions_agent_model(model_id) is True
    assert is_deep_research_model(model_id) is False


def test_gemini_chat_model_is_not_an_interactions_agent() -> None:
    """The agent's own reasoning model, used directly, is an ordinary chat model."""
    assert is_interactions_agent_model("gemini-3.8-flash") is False


@pytest.mark.asyncio
async def test_antigravity_stream_sends_agent_config_and_system_instruction(
    sdk: _SdkCalls,
) -> None:
    """The interactive path submits the agent id, its model, and a separate prompt."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    sdk.create.return_value = _completing_stream("inter_ag_1", "Done.")

    events = await _drain_stream(
        client,
        [
            SystemMessage(content="You are an autonomous task agent."),
            UserMessage(content="Chart the attached numbers."),
        ],
    )

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["agent"] == ANTIGRAVITY_AGENT_ID
    assert call_kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.8-flash",
    }
    assert call_kwargs["background"] is True
    assert call_kwargs["stream"] is True
    # The prompt rides in system_instruction, so the task the agent plans
    # against is only the user's request.
    assert call_kwargs["input"] == "Chart the attached numbers."
    assert "You are an autonomous task agent." in call_kwargs["system_instruction"]

    content_events = [e for e in events if e.type == "content"]
    assert [e.content for e in content_events] == ["Done."]


@pytest.mark.asyncio
async def test_antigravity_agent_config_omits_unset_fields(
    sdk: _SdkCalls,
) -> None:
    """Without configured overrides the API's own defaults are left to apply."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    sdk.create.return_value = _completing_stream("inter_ag_2", "Done.")

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["agent_config"] == {"type": "antigravity"}
    assert "system_instruction" not in call_kwargs


@pytest.mark.asyncio
async def test_antigravity_agent_config_carries_max_total_tokens(
    sdk: _SdkCalls,
) -> None:
    """A configured run budget reaches the API."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_max_total_tokens=250_000,
    )
    sdk.create.return_value = _completing_stream("inter_ag_3", "Done.")

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.8-flash",
        "max_total_tokens": 250_000,
    }


@pytest.mark.asyncio
async def test_antigravity_submitted_request_validates_against_the_sdk_request_model(
    sdk: _SdkCalls,
) -> None:
    """The submitted body is what the installed SDK accepts, not just a dict we like."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_max_total_tokens=250_000,
    )
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([
        SystemMessage(content="Be careful."),
        UserMessage(content="Do the thing."),
    ])

    body = _CREATE_INTERACTION_ADAPTER.validate_python(create.call_args.kwargs)
    assert body.agent == ANTIGRAVITY_AGENT_ID
    assert body.agent_config.type == "antigravity"
    assert body.agent_config.model == "gemini-3.8-flash"
    assert body.agent_config.max_total_tokens == 250_000
    assert body.input == "Do the thing."
    assert body.system_instruction == "Be careful."
    assert body.environment.type == "remote"


@pytest.mark.asyncio
async def test_antigravity_requires_non_empty_input(
    sdk: _SdkCalls,
) -> None:
    """A system prompt alone is not a task for an agent that executes work."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    create = _accept_submissions(sdk)

    with pytest.raises(InvalidRequestError, match="Antigravity requires non-empty"):
        await client.start_agent_interaction([SystemMessage(content="Be careful.")])
    create.assert_not_called()


@pytest.mark.asyncio
async def test_start_agent_interaction_submits_antigravity_without_streaming(
    sdk: _SdkCalls,
) -> None:
    """The submit-then-poll path reuses the same agent shaping with stream=False."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    interaction = Interaction(id="inter_ag_submit", status="in_progress")
    sdk.create.return_value = interaction

    result = await client.start_agent_interaction(
        [UserMessage(content="Do the thing.")],
        previous_interaction_id="inter_ag_prev",
    )

    assert result is interaction
    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["stream"] is False
    assert call_kwargs["agent"] == ANTIGRAVITY_AGENT_ID
    assert call_kwargs["agent_config"]["model"] == "gemini-3.8-flash"
    assert call_kwargs["previous_interaction_id"] == "inter_ag_prev"


@pytest.mark.asyncio
async def test_media_injected_as_provider_parts_is_refused_not_dropped(
    sdk: _SdkCalls,
) -> None:
    """An image or PDF injection has no text form the agent could receive.

    The Google adapter injects multimodal attachments as provider `parts`,
    which this path cannot serialize into the single `input` string — so the
    turn fails rather than asking the agent about a file it never got.
    """
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    create = _accept_submissions(sdk)
    injected = UserMessage(content="[System: File from previous tool response]")
    injected.parts = [{"inline_data": {"mime_type": "image/png"}}]

    with pytest.raises(InvalidRequestError, match="cannot read attachments"):
        await client.start_agent_interaction([
            UserMessage(content="Crop the attached image."),
            injected,
        ])
    create.assert_not_called()


@pytest.mark.asyncio
async def test_image_url_content_part_is_refused_not_dropped(
    sdk: _SdkCalls,
) -> None:
    """The same holds for an image_url part reaching the agent path."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    create = _accept_submissions(sdk)

    with pytest.raises(InvalidRequestError, match="cannot read image_url"):
        await client.start_agent_interaction([
            UserMessage(
                content=[
                    TextContentPart(type="text", text="What is in this picture?"),
                    ImageUrlContentPart(
                        type="image_url", image_url={"url": "https://example.com/a.png"}
                    ),
                ]
            )
        ])
    create.assert_not_called()


@pytest.mark.asyncio
async def test_text_shaped_attachment_injection_still_reaches_the_agent(
    sdk: _SdkCalls,
) -> None:
    """A CSV/JSON/text attachment is injected as text, so it must still pass."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([
        UserMessage(content="Transform the attached CSV."),
        UserMessage(content="[System: File from previous tool response]\na,b\n1,2"),
    ])

    submitted_input = create.call_args.kwargs["input"]
    assert "Transform the attached CSV." in submitted_input
    assert "a,b\n1,2" in submitted_input


def _egress_client(
    network: EgressNetworkPayload | EgressResolution,
) -> GoogleGenAIClient:
    """A client whose egress resolver returns a fixed resolution."""
    resolution = (
        network
        if isinstance(network, EgressResolution)
        else EgressResolution(network=network)
    )

    class _FixedResolver:
        async def resolve(self) -> EgressResolution:
            return resolution

    return GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_egress_resolver=_FixedResolver(),
    )


@pytest.mark.asyncio
async def test_egress_policy_reaches_the_interactive_path(
    sdk: _SdkCalls,
) -> None:
    """The stream path previously had nowhere to put an environment at all.

    Egress policy is static profile config rather than per-turn data, so a
    credential configured for `/coder` has to apply when a user runs the slash
    command directly, not only when a delegation submits.
    """
    client = _egress_client({
        "allowlist": [
            {"domain": "*"},
            {
                "domain": "api.github.com",
                "transform": [{"Authorization": "Bearer ghs_x"}],
            },
        ]
    })
    sdk.create.return_value = _completing_stream("inter_ag_env", "Done.")

    await _drain_stream(client, [UserMessage(content="Open a PR.")])

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["environment"] == {
        "type": "remote",
        "network": {
            "allowlist": [
                {"domain": "*"},
                {
                    "domain": "api.github.com",
                    "transform": [{"Authorization": "Bearer ghs_x"}],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_submit_path_uploads_files_into_an_environment(
    sdk: _SdkCalls,
) -> None:
    """Files are uploaded, not inlined: the API rejects an inline source over
    1 MiB of base64, which is any ordinary photo."""
    client = _egress_client("disabled")
    create = _accept_submissions(sdk)
    photo = b"\x89PNG" + bytes(2 * 1024 * 1024)

    await client.start_agent_interaction(
        [UserMessage(content="Analyse the photo.")],
        sandbox_files=[
            SandboxFile(
                path="/workspace/photo.png", content=photo, mime_type="image/png"
            )
        ],
    )

    sdk.upload.assert_awaited_once_with(
        client.client.aio.environments.files,
        environment="env_1",
        path="/workspace/photo.png",
        file=photo,
        mime_type="image/png",
    )
    assert create.call_args.kwargs["environment"] == {
        "type": "remote",
        "environment_id": "env_1",
    }
    body = _CREATE_INTERACTION_ADAPTER.validate_python(create.call_args.kwargs)
    assert body.environment.environment_id == "env_1"


@pytest.mark.asyncio
async def test_uploaded_environment_carries_the_egress_policy(
    sdk: _SdkCalls,
) -> None:
    """A run on an existing environment ignores ``env`` sent with the run, so
    network and env are set when the environment is created."""
    client = _egress_client(_STORED_GITHUB)
    _accept_submissions(sdk)

    await client.start_agent_interaction(
        [UserMessage(content="Fix the bug in this file.")],
        sandbox_files=[
            SandboxFile(
                path="/workspace/a.py", content=b"x = 1\n", mime_type="text/x-python"
            )
        ],
    )

    sdk.create_environment.assert_awaited_once_with(
        client.client.aio.environments,
        network=_STORED_GITHUB.network,
        extra_body={"env": _STORED_GITHUB.env},
    )


@pytest.mark.asyncio
async def test_a_rejected_upload_fails_the_delegation_permanently(
    sdk: _SdkCalls,
) -> None:
    """Provisioning reaches Google, so its errors use the delegation taxonomy."""
    client = _egress_client("disabled")
    rejection = Exception("Request contains an invalid argument.")
    rejection.status_code = 400  # pyright: ignore[reportAttributeAccessIssue]
    sdk.upload.side_effect = rejection
    create = _accept_submissions(sdk)

    with pytest.raises(DelegationPermanentError):
        await client.start_agent_interaction(
            [UserMessage(content="Analyse the file.")],
            sandbox_files=[
                SandboxFile(
                    path="/workspace/a.csv", content=b"a,b", mime_type="text/csv"
                )
            ],
        )
    create.assert_not_called()


@pytest.mark.asyncio
async def test_default_sandbox_is_stated_rather_than_omitted(
    sdk: _SdkCalls,
) -> None:
    """The shipped profile mounts nothing and sets no egress policy.

    Omitting ``environment`` entirely does not get the API's default sandbox --
    it gets ``400 Missing required field 'environment'``, which failed every
    run on a profile that configures no ``antigravity_config.environment``.
    """
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    interaction = Interaction(id="inter_ag_plain", status="in_progress")
    sdk.create.return_value = interaction

    await client.start_agent_interaction([UserMessage(content="Do the thing.")])

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["environment"] == {"type": "remote"}


@pytest.mark.asyncio
async def test_interactive_path_also_states_the_default_sandbox(
    sdk: _SdkCalls,
) -> None:
    """`/coder` run directly hit the same rejection as a delegated run."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    sdk.create.return_value = _completing_stream("inter_ag_plain_stream", "Done.")

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["environment"] == {"type": "remote"}


@pytest.mark.asyncio
async def test_environment_with_egress_validates_against_the_sdk_request_model(
    sdk: _SdkCalls,
) -> None:
    """The transform shape is what the installed SDK accepts, not just a dict we like."""
    client = _egress_client({
        "allowlist": [
            {
                "domain": "github.com",
                "transform": [{"Authorization": "Basic eC1hY2Nlc3M="}],
            }
        ]
    })
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([UserMessage(content="Clone the repo.")])

    body = _CREATE_INTERACTION_ADAPTER.validate_python(create.call_args.kwargs)
    assert body.environment.type == "remote"
    assert body.environment.network.allowlist[0].domain == "github.com"
    assert body.environment.network.allowlist[0].transform == [
        {"Authorization": "Basic eC1hY2Nlc3M="}
    ]


_STORED_GITHUB = EgressResolution(
    network={
        "allowlist": [
            {"domain": "api.github.com", "credential": "fa-egress-github-app-1"},
            {"domain": "github.com"},
        ]
    },
    env={GITHUB_GIT_AUTH_ENV: {"credential": "fa-egress-github-app-1-git"}},
    github_git=True,
)


@pytest.mark.asyncio
async def test_a_stored_git_credential_is_bound_to_the_sandbox_variable(
    sdk: _SdkCalls,
) -> None:
    """The request the installed SDK accepts carries the credential binding."""
    client = _egress_client(_STORED_GITHUB)
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([UserMessage(content="Fix the bug.")])

    body = _CREATE_INTERACTION_ADAPTER.validate_python(create.call_args.kwargs)
    assert body.environment.model_dump(exclude_none=True)["env"] == {
        GITHUB_GIT_AUTH_ENV: {"credential": "fa-egress-github-app-1-git"}
    }


@pytest.mark.asyncio
async def test_a_stored_git_credential_tells_the_agent_to_configure_git(
    sdk: _SdkCalls,
) -> None:
    """Git sends nothing until the variable is in its header, so the binding
    is useless unless the agent is told to put it there."""
    client = _egress_client(_STORED_GITHUB)
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([
        SystemMessage(content="You are a coding agent."),
        UserMessage(content="Fix the bug."),
    ])

    system_instruction = create.call_args.kwargs["system_instruction"]
    assert "You are a coding agent." in system_instruction
    assert (
        "'http.https://github.com/.extraHeader' "
        f'"Authorization: Basic ${GITHUB_GIT_AUTH_ENV}"'
    ) in system_instruction


@pytest.mark.asyncio
async def test_without_a_stored_git_credential_git_is_not_mentioned(
    sdk: _SdkCalls,
) -> None:
    """A header-only rule carries its own Authorization, which the proxy would
    write over anything git sent."""
    client = _egress_client({
        "allowlist": [
            {
                "domain": "github.com",
                "transform": [{"Authorization": "Basic eC1hY2Nlc3M="}],
            }
        ]
    })
    create = _accept_submissions(sdk)

    await client.start_agent_interaction([
        SystemMessage(content="You are a coding agent."),
        UserMessage(content="Fix the bug."),
    ])

    call_kwargs = create.call_args.kwargs
    assert "env" not in call_kwargs["environment"]
    assert call_kwargs["system_instruction"] == "You are a coding agent."


def _connection_blip() -> Exception:
    """What the SDK raises when a connection to the API fails outright."""
    request = httpx.Request("GET", "https://generativelanguage.googleapis.com/")
    cause = httpx.ConnectError("All connection attempts failed", request=request)
    error = _SDK_COMPAT_ERRORS.APIConnectionError(message=str(cause), request=request)
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
async def test_a_connection_blip_while_polling_is_transient(
    sdk: _SdkCalls,
) -> None:
    """One failed poll of a long run must reschedule, not fail the delegation."""
    client = _egress_client("disabled")
    sdk.get.side_effect = _connection_blip()

    with pytest.raises(DelegationTransientError):
        await client.get_agent_interaction("inter_long_running")


@pytest.mark.asyncio
async def test_a_connection_blip_storing_an_egress_credential_is_transient(
    sdk: _SdkCalls,
) -> None:
    """The credential store is reached over the same network as the run."""
    request = httpx.Request("PATCH", "https://generativelanguage.googleapis.com/")

    class _BlippingResolver:
        async def resolve(self) -> EgressResolution:
            try:
                raise httpx.ConnectError(
                    "All connection attempts failed", request=request
                )
            except httpx.ConnectError as e:
                raise AntigravityEgressError("Storing egress credential failed") from e

    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_egress_resolver=_BlippingResolver(),
    )
    create = _accept_submissions(sdk)

    with pytest.raises(DelegationTransientError):
        await client.start_agent_interaction([UserMessage(content="Fix the bug.")])
    create.assert_not_called()


@pytest.mark.asyncio
async def test_a_misconfigured_egress_credential_still_fails_fast(
    sdk: _SdkCalls,
) -> None:
    """Only a failed connection is retried; a configuration error is not."""

    class _MisconfiguredResolver:
        async def resolve(self) -> EgressResolution:
            raise AntigravityEgressError("GitHub App private key is missing")

    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_egress_resolver=_MisconfiguredResolver(),
    )
    _accept_submissions(sdk)

    with pytest.raises(AntigravityEgressError):
        await client.start_agent_interaction([UserMessage(content="Fix the bug.")])
