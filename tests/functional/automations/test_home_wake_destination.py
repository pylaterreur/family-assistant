"""Wakes created in the web or iOS app land in their owner's Home.

The destination is written onto an automation, reminder or callback when it is
created, and resolved to a conversation when it fires. Automations stored
before Home existed carry no destination and keep firing where they were made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant.actions import ActionType, execute_action
from family_assistant.config_models import (
    AppConfig,
    ConversationThreadsConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.threads.home import ensure_web_home, find_home
from family_assistant.tools import CompositeToolsProvider, LocalToolsProvider
from family_assistant.tools.automations import (
    create_automation_tool,
    update_automation_tool,
)
from family_assistant.tools.tasks import (
    schedule_future_callback_tool,
    schedule_reminder_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import NotificationMetadata
    from family_assistant.storage.types import ActionConfig

SOURCE_CONVERSATION_ID = "web_conv_source"
TELEGRAM_CHAT_ID = "123456"
OWNER = "alice@example.com"


def _processing_service(*, threads: bool) -> ProcessingService:
    tools_provider = CompositeToolsProvider(
        providers=[LocalToolsProvider(definitions=[], implementations={})]
    )
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="Here is your briefing.")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="default_assistant",
            prompts={"system_prompt": "Assistant"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(
            conversation_threads=ConversationThreadsConfig(enabled=threads)
        ),
        context_providers=[],
        server_url=None,
    )


async def _start_worker(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    service: ProcessingService,
    chat_interface: ChatInterface | None = None,
) -> tuple[ChatInterface, asyncio.Event]:
    await service.tools_provider.get_tool_definitions()
    if chat_interface is None:
        mock_interface = AsyncMock(spec=ChatInterface)
        mock_interface.send_message.return_value = "sent_message_id"
        chat_interface = mock_interface
    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=service,
        chat_interface=chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("llm_callback", handle_llm_callback)
    task_worker.register_task_handler("script_execution", handle_script_execution)
    return chat_interface, new_task_event


def _exec_context(
    db: Database,
    service: ProcessingService,
    *,
    interface_type: str = "web",
    conversation_id: str = SOURCE_CONVERSATION_ID,
    user_id: str | None = OWNER,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=interface_type,
        conversation_id=conversation_id,
        user_name="Alice",
        user_id=user_id,
        turn_id="creating_turn",
        db_context=db,
        processing_service=service,
        processing_profile_id="default_assistant",
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _sent_conversation_ids(chat_interface: ChatInterface) -> list[str]:
    assert isinstance(chat_interface, AsyncMock)
    return [
        call.kwargs["conversation_id"]
        for call in chat_interface.send_message.call_args_list
    ]


async def _fire_pending(
    db_engine: AsyncEngine, new_task_event: asyncio.Event, task_type: str
) -> None:
    """Make every pending task of ``task_type`` due now, then wait for them.

    Waits on the tasks by id: a schedule's next occurrence is enqueued as its
    own future task, which a wait over the whole task type would never see
    finish.
    """
    db = Database(engine=db_engine)
    rows = await db.fetch_all(
        select(tasks_table.c.task_id).where(
            tasks_table.c.task_type == task_type,
            tasks_table.c.status == "pending",
        )
    )
    task_ids = {row["task_id"] for row in rows}
    assert task_ids, f"no pending {task_type} task"
    await db.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id.in_(task_ids))
        .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_ids=task_ids)


async def _create_daily_wake(
    db: Database,
    service: ProcessingService,
    action_config: dict[str, object],
    **context_kwargs: str,
) -> None:
    result = await create_automation_tool(
        exec_context=_exec_context(db, service, **context_kwargs),
        name="Morning briefing",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config=dict(action_config),
    )
    assert "Created schedule automation" in result.get_text(), result.get_text()


async def _home(db: Database, service: ProcessingService) -> str | None:
    return await find_home(db, service.app_config, user_id=OWNER, interface_type="web")


@pytest.mark.asyncio
async def test_a_new_web_automation_fires_in_a_home_designated_for_it(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    assert await _home(db, service) is None

    await _create_daily_wake(db, service, {"context": "Send the morning briefing"})
    [automation] = await db.schedule_automations.list_all(SOURCE_CONVERSATION_ID)
    assert automation["action_config"].get("conversation") == "home"
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    home = await _home(db, service)
    assert home is not None
    assert _sent_conversation_ids(chat_interface) == [home]


@pytest.mark.asyncio
async def test_a_new_web_automation_fires_in_the_existing_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    home = await ensure_web_home(
        db, service.app_config, user_id=OWNER, now=datetime.now(UTC)
    )

    await _create_daily_wake(db, service, {"context": "Send the morning briefing"})
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [home]
    source_rows = await db.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.conversation_id == SOURCE_CONVERSATION_ID
        )
    )
    assert source_rows == []


@pytest.mark.asyncio
async def test_an_explicit_source_automation_fires_where_it_was_created(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _create_daily_wake(
        db,
        service,
        {"context": "Send the morning briefing", "conversation": "source"},
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]
    assert await _home(db, service) is None


@pytest.mark.asyncio
async def test_an_automation_stored_without_a_destination_fires_in_its_source(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A row from before Home existed names no destination and stays put."""
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    legacy_config: ActionConfig = {"context": "Send the morning briefing"}

    await db.schedule_automations.create(
        name="Legacy briefing",
        recurrence_rule="FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
        action_type="wake_llm",
        action_config=legacy_config,
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        timezone=ZoneInfo("UTC"),
        processing_profile_id="default_assistant",
        created_by_user_id=OWNER,
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]


@pytest.mark.asyncio
async def test_editing_an_automation_keeps_its_destination(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    await _start_worker(task_worker_manager, service)
    await _create_daily_wake(db, service, {"context": "Send the morning briefing"})
    [automation] = await db.schedule_automations.list_all(SOURCE_CONVERSATION_ID)

    result = await update_automation_tool(
        exec_context=_exec_context(db, service),
        automation_id=automation["id"],
        automation_type="schedule",
        action_config={"context": "Send the evening briefing"},
    )
    assert result.data == {"id": automation["id"], "success": True}, result.get_text()

    [updated] = await db.schedule_automations.list_all(SOURCE_CONVERSATION_ID)
    assert updated["action_config"] == {
        "context": "Send the evening briefing",
        "conversation": "home",
    }


@pytest.mark.asyncio
async def test_a_telegram_automation_fires_in_its_chat(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _create_daily_wake(
        db,
        service,
        {"context": "Send the morning briefing"},
        interface_type="telegram",
        conversation_id=TELEGRAM_CHAT_ID,
    )
    [automation] = await db.schedule_automations.list_all(TELEGRAM_CHAT_ID)
    assert "conversation" not in automation["action_config"]
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [TELEGRAM_CHAT_ID]


@pytest.mark.asyncio
async def test_a_telegram_automation_cannot_ask_for_home(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)

    result = await create_automation_tool(
        exec_context=_exec_context(
            db, service, interface_type="telegram", conversation_id=TELEGRAM_CHAT_ID
        ),
        name="Morning briefing",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "Brief me", "conversation": "home"},
    )

    assert "only supported for the web app" in result.get_text()
    assert await db.schedule_automations.list_all(TELEGRAM_CHAT_ID) == []


@pytest.mark.asyncio
async def test_without_threads_a_web_automation_fires_where_it_was_created(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=False)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _create_daily_wake(db, service, {"context": "Send the morning briefing"})
    [automation] = await db.schedule_automations.list_all(SOURCE_CONVERSATION_ID)
    assert automation["action_config"] == {"context": "Send the morning briefing"}
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]
    assert await _home(db, service) is None


@pytest.mark.asyncio
async def test_without_threads_home_is_refused_at_creation(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=False)

    result = await create_automation_tool(
        exec_context=_exec_context(db, service),
        name="Morning briefing",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "Brief me", "conversation": "home"},
    )

    assert "threads, which are not enabled" in result.get_text()


@pytest.mark.asyncio
async def test_a_home_wake_fires_in_its_source_once_threads_are_off(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Created while threads were on, fired after an operator turned them off."""
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(
        task_worker_manager, _processing_service(threads=False)
    )

    await _create_daily_wake(
        db, _processing_service(threads=True), {"context": "Brief me"}
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", [None, "home"])
async def test_a_reminder_lands_in_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    conversation: str | None,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    result = await schedule_reminder_tool(
        exec_context=_exec_context(db, service),
        reminder_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        message="Take the bins out",
        conversation=conversation,
    )
    assert result is not None and result.startswith("OK."), result
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [await _home(db, service)]


@pytest.mark.asyncio
async def test_a_reminder_follow_up_stays_in_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    _, new_task_event = await _start_worker(task_worker_manager, service)

    await schedule_reminder_tool(
        exec_context=_exec_context(db, service),
        reminder_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        message="Take the bins out",
        follow_up=True,
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    follow_ups = await db.fetch_all(
        select(tasks_table.c.payload).where(
            tasks_table.c.task_type == "llm_callback",
            tasks_table.c.status == "pending",
        )
    )
    [follow_up] = [row["payload"] for row in follow_ups]
    assert follow_up["conversation_id"] == await _home(db, service)
    assert follow_up["interface_type"] == "web"


@pytest.mark.asyncio
async def test_a_reminder_can_stay_in_its_conversation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await schedule_reminder_tool(
        exec_context=_exec_context(db, service),
        reminder_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        message="Take the bins out",
        conversation="source",
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation", "lands_in_home"), [(None, True), ("source", False)]
)
async def test_a_future_callback_follows_the_same_rule(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    conversation: str | None,
    lands_in_home: bool,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    result = await schedule_future_callback_tool(
        exec_context=_exec_context(db, service),
        callback_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        context="Check whether the download finished",
        conversation=conversation,
    )
    assert result is not None and result.startswith("OK."), result
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    expected = await _home(db, service) if lands_in_home else SOURCE_CONVERSATION_ID
    assert _sent_conversation_ids(chat_interface) == [expected]


@pytest.mark.asyncio
async def test_wakes_created_in_a_thread_stay_in_the_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A thread's reminder belongs to its work; arriving there reopens it."""
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    now = datetime.now(UTC)
    home = await ensure_web_home(db, service.app_config, user_id=OWNER, now=now)
    thread_id = "web_conv_plumber_thread"
    await db.conversation_threads.create_thread(
        conversation_id=thread_id,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=OWNER,
        profile_id="default_assistant",
        title="Plumber",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=None,
        now=now,
    )

    await schedule_reminder_tool(
        exec_context=_exec_context(db, service, conversation_id=thread_id),
        reminder_time=(now + timedelta(hours=1)).isoformat(),
        message="The plumber comes today",
    )
    await _create_daily_wake(
        db, service, {"context": "Chase the quote"}, conversation_id=thread_id
    )
    [automation] = await db.schedule_automations.list_all(thread_id)
    assert "conversation" not in automation["action_config"]
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [thread_id, thread_id]


@pytest.mark.asyncio
async def test_a_telegram_reminder_stays_in_its_chat(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await schedule_reminder_tool(
        exec_context=_exec_context(
            db, service, interface_type="telegram", conversation_id=TELEGRAM_CHAT_ID
        ),
        reminder_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        message="Take the bins out",
    )
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    assert _sent_conversation_ids(chat_interface) == [TELEGRAM_CHAT_ID]


@pytest.mark.asyncio
async def test_a_script_automation_wakes_in_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    result = await create_automation_tool(
        exec_context=_exec_context(db, service),
        name="Script briefing",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="script",
        action_config={"script_code": 'wake_llm("time for the briefing")\n'},
    )
    assert "Created schedule automation" in result.get_text(), result.get_text()
    await _fire_pending(db_engine, new_task_event, "script_execution")
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    assert _sent_conversation_ids(chat_interface) == [await _home(db, service)]


@pytest.mark.asyncio
async def test_a_script_stored_without_a_destination_wakes_its_source(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await execute_action(
        db_ctx=db,
        action_type=ActionType.SCRIPT,
        action_config={"script_code": 'wake_llm("time for the briefing")\n'},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        created_by_user_id=OWNER,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    assert _sent_conversation_ids(chat_interface) == [SOURCE_CONVERSATION_ID]


@pytest.mark.asyncio
async def test_a_failed_home_script_reports_in_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await execute_action(
        db_ctx=db,
        action_type=ActionType.SCRIPT,
        action_config={
            "script_code": 'raise ValueError("boom")\n',
            "conversation": "home",
        },
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        created_by_user_id=OWNER,
    )
    await db.execute(
        update(tasks_table)
        .where(tasks_table.c.task_type == "script_execution")
        .values(max_retries=0)
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}, allow_failures=True
    )

    assert _sent_conversation_ids(chat_interface) == [await _home(db, service)]


class _RecordingNotifier:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple[str, str | None]] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Database,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((
            user_identifier,
            metadata.conversation_id if metadata else None,
        ))


@pytest.mark.asyncio
async def test_push_for_a_home_wake_targets_the_new_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """The owner is found even in a Home this wake designated, with no turns yet."""
    db = Database(engine=db_engine)
    service = _processing_service(threads=True)
    notifier = _RecordingNotifier()
    _, new_task_event = await _start_worker(
        task_worker_manager,
        service,
        WebChatInterface(db_engine, notifier=notifier),
    )

    await _create_daily_wake(db, service, {"context": "Send the morning briefing"})
    await _fire_pending(db_engine, new_task_event, "llm_callback")

    home = await _home(db, service)
    assert home is not None
    assert notifier.calls == [(OWNER, home)]
