"""The web chat lands on Home, shows each thread as a live card, and navigates between them."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from playwright.async_api import expect
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.database import Database
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.home_page import HomePage

USER = "test_user"
THREAD_PROFILE = "test_research"
SEED_REQUEST = "Find three plumbers and get quotes for a new hot water system."


@pytest.fixture
def web_conversation_threads_enabled() -> bool:
    return True


@dataclass(frozen=True)
class SeededThread:
    home: str
    thread: str
    delegation_id: str


async def _home_conversation_id(base_url: str) -> str:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/api/v1/chat/home")
    response.raise_for_status()
    home = response.json()["conversation_id"]
    assert isinstance(home, str)
    return home


async def _seed_thread(db: Database, home: str) -> SeededThread:
    """A Home turn that started a thread whose first run is still queued."""
    now = datetime.now(UTC)
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    thread = f"web_conv_{uuid.uuid4()}"
    delegation_id = f"thread-{uuid.uuid4().hex[:8]}"
    home_row = {
        "interface_type": "web",
        "conversation_id": home,
        "timestamp": now,
        "processing_profile_id": "default_assistant",
    }
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Can you get quotes for the hot water?"),
        user_id=USER,
        **home_row,
    )
    await db.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="start_thread",
                        arguments=json.dumps({
                            "target_service_id": THREAD_PROFILE,
                            "title": "Hot water system quotes",
                            "request": SEED_REQUEST,
                        }),
                    ),
                )
            ],
        ),
        **home_row,
    )
    await db.message_history.add_message(
        ToolMessage(
            tool_call_id=call_id, name="start_thread", content="Thread started."
        ),
        **home_row,
    )
    await db.message_history.add_message(
        AssistantMessage(content="On it. I've started a thread for the quotes."),
        **home_row,
    )

    await db.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task-{delegation_id}",
        "source_profile_id": "default_assistant",
        "target_service_id": THREAD_PROFILE,
        "interface_type": "web",
        "conversation_id": thread,
        "subconversation_id": None,
        "run_kind": "thread",
        "request_text": SEED_REQUEST,
        "content_parts_json": [],
        "user_id": USER,
    })
    await db.conversation_threads.create_thread(
        conversation_id=thread,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=USER,
        profile_id=THREAD_PROFILE,
        title="Hot water system quotes",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=delegation_id,
        now=now,
        source_tool_call_id=call_id,
    )
    seed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content=SEED_REQUEST),
        interface_type="web",
        conversation_id=thread,
        timestamp=now,
        processing_profile_id=THREAD_PROFILE,
        user_id=USER,
        is_internal=True,
    )
    await db.conversation_threads.set_seed_message(thread, seed, now)
    return SeededThread(home=home, thread=thread, delegation_id=delegation_id)


async def _open_home(fixture: WebTestFixture, home: str) -> HomePage:
    """Open the chat with no conversation named, once the activity stream is live."""
    page = fixture.page
    chat = HomePage(page, fixture.base_url)
    async with page.expect_request(
        lambda request: "/api/v1/chat/activity/stream" in request.url, timeout=20000
    ):
        await chat.navigate_to_chat()
    await chat.wait_for_home(home)
    return chat


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_home_card_follows_the_thread_from_working_to_waiting(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)

    chat = await _open_home(web_test_fixture, home)

    await chat.wait_for_message_content("On it. I've started a thread for the quotes.")
    card = chat.thread_card(seeded.thread)
    await expect(card).to_be_visible(timeout=15000)
    await chat.wait_for_card_state(seeded.thread, "working")
    await expect(card).to_contain_text("Hot water system quotes")
    await expect(
        chat.sidebar_group("working").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()

    now = datetime.now(UTC)
    assert await db.delegation_runs.mark_completed(
        delegation_id=seeded.delegation_id,
        result_text=None,
        result_attachment_ids=[],
        completed_at=now,
    )
    await db.message_history.add_message(
        AssistantMessage(content="Gas or heat pump?"),
        interface_type="web",
        conversation_id=seeded.thread,
        timestamp=now,
        processing_profile_id=THREAD_PROFILE,
    )
    app = web_test_fixture.assistant.fastapi_app
    assert app is not None
    await app.state.conversation_stream_hub.publish_activity(
        seeded.thread, user_id=USER, reason="thread_run_ended"
    )

    await chat.wait_for_card_state(seeded.thread, "waiting")
    await expect(card).to_contain_text("Gas or heat pump?")
    await expect(
        chat.sidebar_group("waiting").get_by_test_id(f"thread-item-{seeded.thread}")
    ).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_a_card_opens_its_thread_and_the_breadcrumb_returns_home(
    web_test_fixture: WebTestFixture, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home_conversation_id(web_test_fixture.base_url)
    seeded = await _seed_thread(db, home)

    chat = await _open_home(web_test_fixture, home)
    await chat.open_thread_from_card(seeded.thread)

    banner = chat.thread_banner()
    await expect(banner.locator(chat.THREAD_ORIGIN)).to_contain_text(
        f"Assistant asked: “{SEED_REQUEST}”"
    )
    await expect(banner.locator(chat.THREAD_WORKING_INDICATOR)).to_be_visible()
    await expect(chat.heading()).to_have_text("Hot water system quotes")
    await expect(
        web_test_fixture.page.get_by_role("combobox", name="Processing profile")
    ).to_be_disabled()

    await chat.back_to_home(home)
    await expect(chat.thread_card(seeded.thread)).to_be_visible()
    await expect(chat.thread_banner()).to_have_count(0)
