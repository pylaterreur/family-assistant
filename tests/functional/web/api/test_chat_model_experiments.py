"""Live model experiments, end to end through the web chat API.

A turn nobody chose a tier for is assigned an arm by hashing its conversation,
served by that arm's client, and recorded with the experiment on its reply. A
person's choice of a tier no experiment names wins, and an assignment never
turns into a sticky choice of its own. An experiment naming a tier swaps even a
person's choice of it for an arm, and the choice stays sticky.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.config_models import ExperimentConfig
from family_assistant.llm import LLMOutput
from family_assistant.llm.experiments import assign_arm
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
)
from family_assistant.processing import ProcessingService
from family_assistant.storage.database import Database
from family_assistant.utils.clock import MockClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import MessageReasoningInfo

EXPERIMENT = "argon-vs-opus"
ARMS = {"argon": 1, "opus": 1}
FRONTIER_EXPERIMENT = "fable-vs-astra"
FRONTIER_ARMS = {"fable": 1, "astra": 1}

_ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(id="standard", label="Standard"),
        ModelTierOption(id="deep", label="Deep"),
        ModelTierOption(id="frontier", label="Frontier"),
    ),
    auto=frozenset({"standard", "deep"}),
    delegation=(
        ModelTierOption(id="argon", label="Argon"),
        ModelTierOption(id="opus", label="Opus"),
        ModelTierOption(id="fable", label="Fable"),
        ModelTierOption(id="astra", label="Astra"),
    ),
)


def _client_saying(reply: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=reply))


@pytest.fixture(name="clock")
def clock_fixture() -> MockClock:
    return MockClock(datetime(2026, 10, 22, 1, 0, tzinfo=UTC))


@pytest.fixture(name="experiment_app")
def experiment_app_fixture(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
    clock: MockClock,
) -> FastAPI:
    config = api_test_processing_service.service_config
    config.tier_eligibility = _ELIGIBILITY
    app_config = api_test_processing_service.app_config.model_copy(
        update={
            "experiments": [
                ExperimentConfig(id=EXPERIMENT, profiles=[config.id], arms=ARMS),
                ExperimentConfig(
                    id=FRONTIER_EXPERIMENT,
                    profiles=[config.id],
                    tiers=["frontier"],
                    arms=FRONTIER_ARMS,
                ),
            ]
        }
    )
    tier_clients = {
        tier: _client_saying(f"{tier} served this")
        for tier in ("standard", "deep", "frontier", "argon", "opus", "fable", "astra")
    }
    service = ProcessingService(
        llm_client=tier_clients["standard"],
        tools_provider=api_test_processing_service.tools_provider,
        service_config=config,
        context_providers=api_test_processing_service.context_providers,
        server_url="http://testserver",
        app_config=app_config,
        attachment_registry=api_test_processing_service.attachment_registry,
        tier_llm_clients=tier_clients,
        clock=clock,
    )
    app_fixture.state.processing_service = service
    app_fixture.state.processing_services = {config.id: service}
    return app_fixture


async def _send(
    client: AsyncClient, conversation_id: str, model_tier: str | None = None
) -> str:
    body: dict[str, str] = {"prompt": "next", "conversation_id": conversation_id}
    if model_tier is not None:
        body["model_tier"] = model_tier
    response = await client.post("/api/v1/chat/send_message", json=body)
    assert response.status_code == 200
    return response.json()["reply"]


async def _latest_reasoning(
    db_engine: AsyncEngine, conversation_id: str
) -> MessageReasoningInfo:
    rows = await Database(engine=db_engine).message_history.get_recent_with_metadata(
        interface_type="api", conversation_id=conversation_id, limit=10
    )
    reasoning = [row for row in rows if row["role"] == "assistant"][-1][
        "reasoning_info"
    ]
    assert reasoning is not None
    return cast("MessageReasoningInfo", reasoning)


def _conversation_on(
    arm: str, experiment: str = EXPERIMENT, arms: dict[str, int] = ARMS
) -> str:
    """A conversation id *experiment* assigns to *arm*."""
    while True:
        conversation_id = f"exp-{uuid.uuid4()}"
        if assign_arm(experiment, conversation_id, arms) == arm:
            return conversation_id


@pytest.mark.usefixtures("experiment_app")
@pytest.mark.parametrize("arm", ["argon", "opus"])
async def test_an_unchosen_turn_runs_on_its_conversations_arm(
    api_test_client: AsyncClient, db_engine: AsyncEngine, arm: str
) -> None:
    conversation_id = _conversation_on(arm)

    assert await _send(api_test_client, conversation_id) == f"{arm} served this"
    assert await _send(api_test_client, conversation_id) == f"{arm} served this"

    reasoning = await _latest_reasoning(db_engine, conversation_id)
    assert reasoning.get("model_tier") == arm
    assert reasoning.get("model_tier_source") == "experiment"
    assert reasoning.get("experiment_id") == EXPERIMENT
    assert reasoning.get("experiment_arm") == arm


@pytest.mark.usefixtures("experiment_app")
async def test_an_assignment_never_becomes_a_sticky_choice(
    api_test_client: AsyncClient,
) -> None:
    conversation_id = _conversation_on("argon")
    await _send(api_test_client, conversation_id)

    response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        params={"include_conversation_profile": "true"},
    )

    assert response.json()["conversation_model_tier"] is None


@pytest.mark.usefixtures("experiment_app")
async def test_a_persons_choice_wins_and_sticks(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    conversation_id = _conversation_on("argon")

    assert await _send(api_test_client, conversation_id, "deep") == "deep served this"
    assert await _send(api_test_client, conversation_id) == "deep served this"

    reasoning = await _latest_reasoning(db_engine, conversation_id)
    assert reasoning.get("model_tier_source") == "user"
    assert "experiment_id" not in reasoning


@pytest.mark.usefixtures("experiment_app")
@pytest.mark.parametrize("arm", ["fable", "astra"])
async def test_an_experiment_on_a_tier_takes_a_persons_choice_of_it(
    api_test_client: AsyncClient, db_engine: AsyncEngine, arm: str
) -> None:
    conversation_id = _conversation_on(arm, FRONTIER_EXPERIMENT, FRONTIER_ARMS)

    served = await _send(api_test_client, conversation_id, "frontier")
    assert served == f"{arm} served this"
    # The choice of Frontier stays sticky, and keeps landing on the same arm.
    assert await _send(api_test_client, conversation_id) == f"{arm} served this"

    reasoning = await _latest_reasoning(db_engine, conversation_id)
    assert reasoning.get("model_tier") == arm
    assert reasoning.get("model_tier_requested") == "frontier"
    assert reasoning.get("model_tier_source") == "experiment"
    assert reasoning.get("experiment_id") == FRONTIER_EXPERIMENT
    response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        params={"include_conversation_profile": "true"},
    )
    assert response.json()["conversation_model_tier"] == "frontier"


async def test_home_is_drawn_again_each_day(
    experiment_app: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    clock: MockClock,
) -> None:
    """Home never ends, so one draw would hold it on one arm for the whole run."""
    home = await Database(engine=db_engine).conversation_threads.designate_home(
        user_id="home-owner",
        interface_type="web",
        conversation_id="exp-home",
        now=datetime.now(UTC),
    )
    service = cast("ProcessingService", experiment_app.state.processing_service)
    timezone = service.service_config.timezone
    served: set[str] = set()
    for _ in range(10):
        today = clock.now().astimezone(timezone).date()
        expected = assign_arm(EXPERIMENT, f"{home}:{today.isoformat()}", ARMS)
        assert await _send(api_test_client, home) == f"{expected} served this"
        assert await _send(api_test_client, home) == f"{expected} served this"
        served.add(expected)
        clock.advance(timedelta(days=1))

    assert served == set(ARMS)
