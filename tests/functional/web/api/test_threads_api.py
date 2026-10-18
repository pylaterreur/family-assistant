"""The Home and threads endpoints."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from family_assistant.config_models import AppConfig, ConversationThreadsConfig
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing import ProcessingService
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import get_current_user
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
CALLER = "test_user"


@pytest.fixture
def threads_enabled(app_fixture: FastAPI) -> None:
    app_fixture.state.config = AppConfig(
        database_url=app_fixture.state.config.database_url,
        conversation_threads=ConversationThreadsConfig(enabled=True),
    )


async def _as_other_user(app_fixture: FastAPI) -> None:
    async def other_user() -> dict[str, object]:
        return {"user_identifier": "someone_else"}

    app_fixture.dependency_overrides[get_current_user] = other_user


async def _add_thread(db: Database, home: str) -> str:
    """A thread of ``home`` whose starting run has finished with a reply."""
    thread = "web_conv_thread"
    await db.conversation_threads.create_thread(
        conversation_id=thread,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=CALLER,
        profile_id="browser_profile",
        title="Hot water system quotes",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=None,
        now=NOW,
        source_tool_call_id="call_123",
    )
    seed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Find three plumbers."),
        interface_type="web",
        conversation_id=thread,
        timestamp=NOW,
        processing_profile_id="browser_profile",
        user_id=CALLER,
        is_internal=True,
    )
    await db.conversation_threads.set_seed_message(thread, seed, NOW)
    await db.message_history.add_message(
        AssistantMessage(content="Gas or heat pump?"),
        interface_type="web",
        conversation_id=thread,
        timestamp=NOW,
        processing_profile_id="browser_profile",
    )
    return thread


@pytest.mark.asyncio
async def test_home_is_absent_while_threads_are_disabled(
    api_test_client: AsyncClient,
) -> None:
    response = await api_test_client.get("/api/v1/chat/home")

    assert response.json() == {"enabled": False, "conversation_id": None}


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_home_is_the_same_conversation_every_time(
    api_test_client: AsyncClient,
) -> None:
    first = (await api_test_client.get("/api/v1/chat/home")).json()
    second = (await api_test_client.get("/api/v1/chat/home")).json()

    assert first["conversation_id"] is not None
    assert first["conversation_id"] == second["conversation_id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_each_user_has_their_own_home(
    app_fixture: FastAPI, api_test_client: AsyncClient
) -> None:
    mine = (await api_test_client.get("/api/v1/chat/home")).json()
    await _as_other_user(app_fixture)
    try:
        theirs = (await api_test_client.get("/api/v1/chat/home")).json()
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert mine["conversation_id"] != theirs["conversation_id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_threads_list_what_home_shows_about_each_thread(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(Database(engine=db_engine), home)

    response = await api_test_client.get("/api/v1/chat/home/threads")

    body = response.json()
    assert body["home_conversation_id"] == home
    assert [
        {
            key: entry[key]
            for key in (
                "conversation_id",
                "title",
                "profile_id",
                "state",
                "source_tool_call_id",
                "seed_request",
                "preview",
            )
        }
        for entry in body["threads"]
    ] == [
        {
            "conversation_id": thread,
            "title": "Hot water system quotes",
            "profile_id": "browser_profile",
            "state": "waiting",
            "source_tool_call_id": "call_123",
            "seed_request": "Find three plumbers.",
            "preview": "Gas or heat pump?",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_threads_never_list_another_users_home(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    await _add_thread(Database(engine=db_engine), home)

    await _as_other_user(app_fixture)
    try:
        response = await api_test_client.get("/api/v1/chat/home/threads")
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.json()["threads"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_thread_reports_its_kind_and_card(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(Database(engine=db_engine), home)

    body = (
        await api_test_client.get(
            f"/api/v1/chat/conversations/{thread}/messages",
            params={"include_conversation_profile": "true"},
        )
    ).json()

    assert (
        body["conversation_kind"],
        body["latest_user_profile_id"],
        body["thread"]["home_conversation_id"],
        body["thread"]["title"],
    ) == ("thread", "browser_profile", home, "Hot water system quotes")


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_home_reports_its_kind(api_test_client: AsyncClient) -> None:
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]

    body = (
        await api_test_client.get(
            f"/api/v1/chat/conversations/{home}/messages",
            params={"include_conversation_profile": "true"},
        )
    ).json()

    assert body["conversation_kind"] == "home"


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_turn_in_a_thread_runs_on_the_threads_profile(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> None:
    seen: list[list[LLMMessage]] = []
    thread = await _thread_on_its_own_profile(
        app_fixture, api_test_client, api_test_processing_service, db_engine, seen
    )
    default_id = api_test_processing_service.service_config.id

    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "Gas, please.",
            "conversation_id": thread,
            "profile_id": default_id,
            "interface_type": "web",
        },
    )

    assert response.json()["reply"] == "the thread's profile answered"
    assert any("Find three plumbers." in str(m.content) for m in seen[0])


async def _thread_on_its_own_profile(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
    seen: list[list[LLMMessage]] | None = None,
) -> str:
    """A thread on browser_profile, whose model answers and records its input."""
    seen = seen if seen is not None else []

    def thread_reply(kwargs: MatcherArgs) -> LLMOutput:
        seen.append(list(kwargs.get("messages", [])))
        return LLMOutput(content="the thread's profile answered")

    thread_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(rules=[(lambda _kwargs: True, thread_reply)]),
        tools_provider=api_test_processing_service.tools_provider,
        service_config=replace(
            api_test_processing_service.service_config, id="browser_profile"
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=api_test_processing_service.app_config,
    )
    default_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {
        default_id: api_test_processing_service,
        "browser_profile": thread_service,
        # A delegation-only profile, which direct chat refuses by name.
        "remote_only": SimpleNamespace(kind="remote"),
    }
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    return await _add_thread(Database(engine=db_engine), home)


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_thread_ignores_a_requested_profile_it_would_refuse(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> None:
    thread = await _thread_on_its_own_profile(
        app_fixture, api_test_client, api_test_processing_service, db_engine
    )

    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "Gas, please.",
            "conversation_id": thread,
            "profile_id": "remote_only",
            "interface_type": "web",
        },
    )

    assert response.json()["reply"] == "the thread's profile answered"


async def _say(db: Database, conversation_id: str) -> None:
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="hello"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=NOW,
        user_id=CALLER,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_the_conversation_list_marks_homes_and_threads(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    await _say(db, home)
    thread = await _add_thread(db, home)
    await _say(db, "web_conv_ordinary")

    body = (await api_test_client.get("/api/v1/chat/conversations")).json()

    assert {
        entry["conversation_id"]: entry["kind"] for entry in body["conversations"]
    } == {
        home: "home",
        thread: "thread",
        "web_conv_ordinary": "conversation",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_the_ordinary_filter_leaves_out_homes_and_threads(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    await _say(db, home)
    await _add_thread(db, home)
    await _say(db, "web_conv_ordinary")

    body = (
        await api_test_client.get(
            "/api/v1/chat/conversations", params={"kind": "ordinary"}
        )
    ).json()

    assert (
        [entry["conversation_id"] for entry in body["conversations"]],
        body["count"],
    ) == (["web_conv_ordinary"], 1)


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_background_work_a_thread_handed_off_keeps_it_working(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)
    await db.delegation_runs.create_run({
        "delegation_id": "delegation_background",
        "task_id": "task_background",
        "source_profile_id": "browser_profile",
        "target_service_id": "research",
        "interface_type": "web",
        "conversation_id": thread,
        "subconversation_id": "sub-background",
        "request_text": "Compare the quotes.",
        "content_parts_json": [{"type": "text", "text": "Compare the quotes."}],
    })

    body = (await api_test_client.get("/api/v1/chat/home/threads")).json()

    assert [entry["state"] for entry in body["threads"]] == ["working"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_turn_naming_no_interface_still_sees_the_thread(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> None:
    seen: list[list[LLMMessage]] = []
    thread = await _thread_on_its_own_profile(
        app_fixture, api_test_client, api_test_processing_service, db_engine, seen
    )

    await api_test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Gas, please.", "conversation_id": thread},
    )

    assert any("Find three plumbers." in str(m.content) for m in seen[0])
