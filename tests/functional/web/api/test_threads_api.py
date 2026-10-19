"""The Home and threads endpoints."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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


def _a_minute_ago() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=1)


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


async def _add_thread(db: Database, home: str, *, at: datetime = NOW) -> str:
    """A thread of ``home`` whose starting run has finished with a reply at ``at``."""
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
        now=at,
        source_tool_call_id="call_123",
    )
    seed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Find three plumbers."),
        interface_type="web",
        conversation_id=thread,
        timestamp=at,
        processing_profile_id="browser_profile",
        user_id=CALLER,
        is_internal=True,
    )
    await db.conversation_threads.set_seed_message(thread, seed, at)
    await db.message_history.add_message(
        AssistantMessage(content="Gas or heat pump?"),
        interface_type="web",
        conversation_id=thread,
        timestamp=at,
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_home_send_runs_on_the_default_profile_whatever_it_names(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> None:
    await _thread_on_its_own_profile(
        app_fixture, api_test_client, api_test_processing_service, db_engine
    )
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]

    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "What's on today?",
            "conversation_id": home,
            "profile_id": "browser_profile",
            "interface_type": "web",
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] != "the thread's profile answered"


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_home_turn_runs_on_the_default_profile_whatever_it_names(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> None:
    await _thread_on_its_own_profile(
        app_fixture, api_test_client, api_test_processing_service, db_engine
    )
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    turn_id = str(uuid.uuid4())

    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": home,
            "prompt": "What's on today?",
            "profile_id": "browser_profile",
            "interface_type": "web",
        },
    )
    assert post.status_code == 200
    stream = await api_test_client.get(
        f"/api/v1/chat/conversations/{home}/stream", params={"from_seq": 0}
    )

    assert "the thread's profile answered" not in stream.text
    row = await Database(engine=db_engine).message_history.get_user_row_by_turn_id(
        turn_id
    )
    assert row is not None
    assert row["processing_profile_id"] == api_test_processing_service.service_config.id


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_user_marking_a_thread_done_writes_no_echo(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home, at=_a_minute_ago())

    response = await api_test_client.post(f"/api/v1/chat/home/threads/{thread}/done")

    body = response.json()
    assert (body["state"], body["closed_by"], body["echo_message_id"]) == (
        "done",
        "user",
        None,
    )
    messages = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]
    assert messages == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_user_closed_thread_reopens_when_a_message_arrives(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home, at=_a_minute_ago())
    await api_test_client.post(f"/api/v1/chat/home/threads/{thread}/done")

    await db.message_history.add_message(
        AssistantMessage(content="Reminder: the plumber comes today."),
        interface_type="web",
        conversation_id=thread,
        timestamp=datetime.now(UTC),
        processing_profile_id="browser_profile",
    )

    [entry] = (await api_test_client.get("/api/v1/chat/home/threads")).json()["threads"]
    assert (entry["state"], entry["closed_by"]) == ("waiting", None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_only_the_owner_can_mark_a_thread_done(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)

    await _as_other_user(app_fixture)
    try:
        done = await api_test_client.post(f"/api/v1/chat/home/threads/{thread}/done")
        read = await api_test_client.post(
            f"/api/v1/chat/conversations/{thread}/read", json={}
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    stored = await db.conversation_threads.get_thread(thread)
    assert (done.status_code, read.status_code) == (404, 404)
    assert stored is not None
    assert stored.close is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_reading_a_thread_clears_the_readers_unread_count(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Gas, please."),
        interface_type="web",
        conversation_id=thread,
        timestamp=NOW,
        processing_profile_id="browser_profile",
        user_id=CALLER,
    )

    before = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    read = await api_test_client.post(
        f"/api/v1/chat/conversations/{thread}/read", json={}
    )
    after = (await api_test_client.get("/api/v1/chat/home/threads")).json()

    # The caller's own message is never unread to them.
    assert [entry["unread_count"] for entry in before["threads"]] == [1]
    assert read.json()["unread_count"] == 0
    assert [entry["unread_count"] for entry in after["threads"]] == [0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_unread_is_counted_per_viewer(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)

    await api_test_client.post(f"/api/v1/chat/conversations/{thread}/read", json={})

    mine = await db.conversation_threads.unread_counts(
        [thread], viewer_user_id=CALLER, viewer_keys={CALLER}
    )
    theirs = await db.conversation_threads.unread_counts(
        [thread], viewer_user_id="partner", viewer_keys={"partner"}
    )
    assert (mine.get(thread, 0), theirs.get(thread, 0)) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_a_read_marker_never_moves_backward(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)
    thread_record = await db.conversation_threads.get_thread(thread)
    assert thread_record is not None and thread_record.seed_message_id is not None

    await api_test_client.post(f"/api/v1/chat/conversations/{thread}/read", json={})
    stale = await api_test_client.post(
        f"/api/v1/chat/conversations/{thread}/read",
        json={"through_message_id": thread_record.seed_message_id},
    )

    assert stale.json()["unread_count"] == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("threads_enabled")
async def test_an_echo_is_attributed_to_its_thread(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    thread = await _add_thread(db, home)
    rows = (
        await api_test_client.get(f"/api/v1/chat/conversations/{thread}/messages")
    ).json()["messages"]
    [reply_id] = [row["internal_id"] for row in rows if row["role"] == "assistant"]
    await db.message_history.copy_message_as_echo(
        source_internal_id=reply_id,
        destination_conversation_id=home,
        destination_profile_id="default_assistant",
        echoed_from_conversation_id=thread,
        turn_id="turn-echo",
        timestamp=NOW,
    )

    messages = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]

    assert [(m["content"], m["echoed_from_thread"]) for m in messages] == [
        ("Gas or heat pump?", thread)
    ]
