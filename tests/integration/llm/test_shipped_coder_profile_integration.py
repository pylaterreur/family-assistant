"""The shipped `coder` profile, run against the real Antigravity agent.

What these pin is what the backend reports it ran, not what our code meant to
send: a delegated run at a tier must be *reasoned with* that tier's model, and
the agent must receive the profile's own prompt rather than the routing blurb
written for whoever picks a profile. Both are read back from the finished
interaction, which echoes the agent configuration and system instruction it
actually used.

The profile comes from the shipped configuration through the public startup
path, so a regression anywhere between `defaults.yaml` and the request --
tier resolution, the per-tier client, the frozen selection reaching submit,
prompt assembly -- shows up here.
"""

import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from google.genai.interactions import AntigravityAgentConfig, Interaction
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.llm.model_selection import ModelSelectionRequest
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interaction_terminal_error_status,
)
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.storage.database import Database
from tests.helpers import wait_for_condition

from .vcr_helpers import sanitize_response

_CURRENT_TIME = re.compile(r"Current time: [^\n\"]*")


def _pin_prompt_clock(request: Any) -> Any:  # noqa: ANN401 # VCR request objects are dynamically typed
    """Make the clock the service folds into the prompt match across runs.

    Applied by VCR to a request both before recording it and before matching
    it, so the recorded and the replayed prompt agree on the time.
    """
    if not request.body:
        return request
    body = json.loads(request.body)
    instruction = body.get("system_instruction")
    if isinstance(instruction, str):
        body["system_instruction"] = _CURRENT_TIME.sub(
            "Current time: <pinned>", instruction
        )
        request.body = json.dumps(body).encode()
    return request


@pytest.fixture(name="vcr_config")
def vcr_config_fixture(vcr_config: dict[str, object]) -> dict[str, object]:
    return {**vcr_config, "before_record_request": _pin_prompt_clock}


@pytest.fixture(name="coder")
async def coder_fixture(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[InteractionsAgentProcessingService]:
    """The `coder` service exactly as the shipped configuration builds it."""
    # Replay needs no real key, and the cassette holds none; startup builds a
    # client for every provider the shipped tiers name, so each needs a value.
    monkeypatch.setenv("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", "test-gemini-key"))
    for env_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(env_var, os.getenv(env_var, f"test-{env_var.lower()}"))
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml", load_dotenv_file=False
    )
    config.service_profiles = [p for p in config.service_profiles if p.id == "coder"]
    config.default_service_profile_id = "coder"
    config.telegram_enabled = False
    config.mcp_config.mcpServers = {}
    config.embedding_model = "mock-deterministic-embedder"
    config.embedding_dimensions = 10

    assistant = Assistant(config=config, database_engine=db_engine)
    try:
        await assistant.setup_dependencies()
        service = assistant.processing_services_registry["coder"]
        assert isinstance(service, InteractionsAgentProcessingService)
        yield service
    finally:
        await assistant.stop_services()


async def _run_to_completion(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    *,
    model_tier: str | None,
    llm_record_mode: str,
) -> Interaction:
    selection = (
        coder.resolve_model_selection(
            ModelSelectionRequest(tier=model_tier, source="model")
        )
        if model_tier is not None
        else None
    )
    submission = await coder.submit_async(
        [{"type": "text", "text": "Run Python to print 17 * 2311, then stop."}],
        conversation_id="coder-integration",
        subconversation_id=None,
        user_name="Tester",
        db_context=Database(db_engine),
        model_selection=selection,
    )
    # Reading a run back goes by its id alone, which any of the profile's
    # clients serves.
    client = coder.llm_client
    assert isinstance(client, GoogleGenAIClient)
    interaction = await client.get_agent_interaction(submission.remote_task_id)

    async def finished() -> bool:
        nonlocal interaction
        interaction = await client.get_agent_interaction(submission.remote_task_id)
        return (
            interaction.status == "completed"
            or is_interaction_terminal_error_status(interaction.status)
        )

    try:
        await wait_for_condition(
            finished,
            timeout=600.0,
            interval=0.01 if llm_record_mode == "replay" else 15.0,
        )
    finally:
        if interaction.status == "in_progress":
            await client.cancel_agent_interaction(submission.remote_task_id)
    assert interaction.status == "completed"
    assert "39287" in (interaction.output_text or "")
    return interaction


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    ("model_tier", "reasoning_model"),
    [(None, "gemini-3.8-flash"), ("gemini_4_argon", "gemini-4-argon")],
    ids=["default", "argon"],
)
async def test_a_delegated_run_is_reasoned_with_its_tiers_model(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
    model_tier: str | None,
    reasoning_model: str,
) -> None:
    """The default is Flash; a delegation that asks for Argon is run on Argon."""
    interaction = await _run_to_completion(
        coder, db_engine, model_tier=model_tier, llm_record_mode=llm_record_mode
    )

    assert isinstance(interaction.agent_config, AntigravityAgentConfig)
    assert interaction.agent_config.model == reasoning_model


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_the_agent_receives_the_profiles_prompt_not_its_routing_blurb(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
) -> None:
    """The description tells a *caller* when to pick coder; the agent never sees it.

    It says "choose it over spawn_worker" in the second person, which the agent
    would read as addressed to itself, offering alternatives it cannot reach.
    The prompt also arrives as a first-class system instruction, so it carries
    no `System:` marker of the kind the inline paths need.
    """
    interaction = await _run_to_completion(
        coder, db_engine, model_tier=None, llm_record_mode=llm_record_mode
    )

    instruction = interaction.system_instruction or ""
    assert "working for Tester" in instruction
    assert "spawn_worker" not in instruction
    assert "complex_tasks" not in instruction
    assert not instruction.startswith("System:")
