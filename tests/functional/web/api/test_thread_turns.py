"""A web thread's first turn runs live in the thread.

See docs/design/one-thread-chat-ux.md: the turn streams like any turn the user
sends, and a message sent while it runs steers it rather than starting another.

The run is created here without its queue task and handed to a worker directly,
so the API app's own worker pool -- wired to a different app state -- never
picks it up.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import AppConfig, ConversationThreadsConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService
from family_assistant.storage import delegation_runs_table
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker
from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    copy_across_boundary,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock
from family_assistant.web.thread_turns import WebThreadTurnRunner
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

TARGET = "complex_tasks"
OWNER = "test_user"
THREAD = "web_conv_thread"
THREAD_REPLY = "Here are three quotes."


@dataclass
class LiveThread:
    client: AsyncClient
    db: Database
    target_gate: asyncio.Event
    turn_id: str

    async def wait_until_running(self) -> None:
        async def running() -> bool:
            rows = await self.db.fetch_all(
                select(delegation_runs_table.c.status).where(
                    delegation_runs_table.c.run_kind == "thread"
                )
            )
            return bool(rows) and rows[0]["status"] == "running"

        await wait_for_condition(running, description="the thread run to start")


async def _create_thread_run(
    db: Database, home: str, target: ProcessingService
) -> tuple[str, str]:
    """A thread of ``home`` with its seed and a queued run, as start_thread makes."""
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    turn_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    await db.conversation_threads.create_thread(
        conversation_id=THREAD,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=OWNER,
        profile_id=TARGET,
        title="Hot water system quotes",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=delegation_id,
        now=now,
    )
    await copy_across_boundary(
        db,
        destination=BoundaryDestination(
            interface_type="web",
            conversation_id=THREAD,
            profile=target.service_config,
            user_id=OWNER,
        ),
        payload=AssistantRequest(text="Find three plumbers.", taint=None),
        turn_id=turn_id,
        now=now,
    )
    await db.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task_{delegation_id}",
        "source_profile_id": "default_assistant",
        "target_service_id": TARGET,
        "interface_type": "web",
        "conversation_id": THREAD,
        "user_id": OWNER,
        "user_name": "Test User",
        "subconversation_id": None,
        "run_kind": "thread",
        "turn_id": turn_id,
        "request_text": "Find three plumbers.",
        "content_parts_json": [{"type": "text", "text": "Find three plumbers."}],
    })
    return delegation_id, turn_id


@pytest.fixture
async def live_thread(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> AsyncGenerator[LiveThread]:
    app_config = AppConfig(
        database_url=app_fixture.state.config.database_url,
        conversation_threads=ConversationThreadsConfig(enabled=True),
    )
    app_fixture.state.config = app_config
    target_gate = asyncio.Event()
    target_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content=THREAD_REPLY),
            response_gate=target_gate,
        ),
        tools_provider=api_test_processing_service.tools_provider,
        service_config=replace(api_test_processing_service.service_config, id=TARGET),
        context_providers=[],
        server_url="http://testserver",
        app_config=app_config,
    )
    registry = {TARGET: target_service}
    target_service.processing_services_registry = registry
    api_test_processing_service.processing_services_registry = registry
    app_fixture.state.processing_services = registry
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    delegation_id, turn_id = await _create_thread_run(db, home, target_service)

    worker = TaskWorker(
        processing_service=api_test_processing_service,
        chat_interface=MagicMock(spec=ChatInterface),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        stream_hub=app_fixture.state.conversation_stream_hub,
        thread_turn_runner=WebThreadTurnRunner(app_fixture.state),
    )
    context = ToolExecutionContext(
        interface_type="web",
        conversation_id=THREAD,
        user_name="Test User",
        user_id=OWNER,
        turn_id=None,
        db_context=db,
        task_priority=TaskPriority.INTERACTIVE,
        processing_service=api_test_processing_service,
        clock=SystemClock(),
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    handler = asyncio.create_task(
        worker.handle_delegated_profile_run(
            context,
            {
                "delegation_id": delegation_id,
                "interface_type": "web",
                "conversation_id": THREAD,
                "user_name": "Test User",
            },
        )
    )
    yield LiveThread(
        client=api_test_client, db=db, target_gate=target_gate, turn_id=turn_id
    )
    target_gate.set()
    await handler


@pytest.mark.asyncio
async def test_the_first_turn_streams_in_the_thread(live_thread: LiveThread) -> None:
    await live_thread.wait_until_running()
    live_thread.target_gate.set()

    response = await live_thread.client.get(
        f"/api/v1/chat/conversations/{THREAD}/stream",
        params={"from_seq": 0, "follow": "false"},
    )

    assert THREAD_REPLY in "".join(
        json.loads(line.removeprefix("data: ")).get("content", "")
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"content"' in line
    )


@pytest.mark.asyncio
async def test_a_message_while_the_first_turn_runs_is_sent_to_steer_it(
    live_thread: LiveThread,
) -> None:
    await live_thread.wait_until_running()

    response = await live_thread.client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": THREAD,
            "prompt": "Gas, please.",
            "interface_type": "web",
        },
    )

    assert (response.status_code, response.json()["detail"]["active_turn_id"]) == (
        409,
        live_thread.turn_id,
    )
