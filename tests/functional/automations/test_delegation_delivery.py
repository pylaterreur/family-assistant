"""A delegation's follow-up turn is launched by a delivery task of its own.

The turn that tells the user about a finished delegation -- the delegating
profile's wake turn -- can do real work: it calls tools, and under
commit-as-you-go every tool's effect is durable the moment it happens. The
delivery task launches it on the background turn runner rather than hosting
it. These tests cover what happens when that turn is cut off partway, by its
process going away: the next delivery continues the turn from the rows it
already persisted, so a tool that already ran does not run again.
"""

import asyncio
import json
import uuid
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import InMemoryTurnTaintTracker
from family_assistant.services.delegation_delivery import (
    DELEGATION_DELIVERY_TASK_TYPE,
    DELEGATION_WAKE_RESUMER,
    DelegationDeliveryPayload,
    DelegationWakeResumer,
    delegation_delivery_task_id,
    queue_delegation_delivery,
)
from family_assistant.services.turn_resumption import (
    MAX_RESUME_ATTEMPTS,
    TURN_RESUME_TASK_TYPE,
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage import message_history_table
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.storage.repositories.tasks import TasksRepository
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.catalog import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    ToolsProvider,
)
from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext
from family_assistant.utils.clock import SystemClock
from tests.helpers import deliver_queued_delegations
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient
from tests.mocks.recording_chat_interface import RecordingChatInterface

if TYPE_CHECKING:
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunCreate,
    )

SOURCE_PROFILE = "source_profile"
INTERFACE = "test_interface"
CONVERSATION = "delegation_delivery_chat"
USER_ID = "delegation-delivery-user"
USER_NAME = "TestUser"
NOTE_TITLE = "Delegated findings"
NOTE_LINE = "The delegated research found three options."
FINAL_REPLY = "I saved the findings to your notes."


def _has_tool_result(messages: list[LLMMessage]) -> bool:
    return any(isinstance(message, ToolMessage) for message in messages)


class _WakeTurnLLM(RuleBasedMockLLMClient):
    """Saves the result to a note, then replies; can hold its second call."""

    def __init__(self) -> None:
        super().__init__(
            rules=[
                (self._after_tool, self._reply),
                (lambda _args: True, self._save_note),
            ]
        )
        self.calls: list[list[LLMMessage]] = []
        self.hold_reply = asyncio.Event()
        self.reply_requested = asyncio.Event()
        self.hold_reply.set()

    @staticmethod
    def _after_tool(args: MatcherArgs) -> bool:
        return _has_tool_result(args["messages"])

    @staticmethod
    def _reply(_args: MatcherArgs) -> LLMOutput:
        return LLMOutput(content=FINAL_REPLY, tool_calls=None)

    @staticmethod
    def _save_note(_args: MatcherArgs) -> LLMOutput:
        return LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments=json.dumps({
                            "title": NOTE_TITLE,
                            "content": NOTE_LINE,
                            "append": True,
                        }),
                    ),
                )
            ],
        )

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self.calls.append(list(messages))
        if _has_tool_result(messages):
            self.reply_requested.set()
            await self.hold_reply.wait()
        return await super().generate_response(messages, tools, tool_choice)

    def hold(self) -> None:
        self.hold_reply.clear()
        self.reply_requested.clear()


def _tools_provider() -> ToolsProvider:
    return PolicyEnforcingToolsProvider(
        wrapped_provider=CompositeToolsProvider(
            providers=[
                LocalToolsProvider(registrations=local_tool_registrations),
                MCPToolsProvider(mcp_server_configs={}),
            ]
        ),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )


async def _source_service(llm: _WakeTurnLLM) -> ProcessingService:
    tools_provider = _tools_provider()
    await tools_provider.get_tool_definitions()
    service = ProcessingService(
        llm_client=llm,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a {profile_id} assistant."},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
            id=SOURCE_PROFILE,
        ),
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    service.processing_services_registry = {SOURCE_PROFILE: service}
    return service


def _worker(
    db_engine: AsyncEngine,
    service: ProcessingService,
    chat_interface: RecordingChatInterface,
    registry: TurnLeaseRegistry | None = None,
) -> TaskWorker:
    return TaskWorker(
        processing_service=service,
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MockEmbeddingGenerator(),
        engine=db_engine,
        chat_interfaces={INTERFACE: chat_interface},
        turn_lease_registry=registry,
    )


def _context(
    db_engine: AsyncEngine,
    service: ProcessingService,
    chat_interface: RecordingChatInterface,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        user_name=USER_NAME,
        user_id=USER_ID,
        turn_id=None,
        db_context=Database(engine=db_engine),
        task_priority=TaskPriority.INTERACTIVE,
        processing_service=service,
        clock=SystemClock(),
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        chat_interfaces={INTERFACE: chat_interface},
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=InMemoryTurnTaintTracker(),
    )


async def _finished_run(db_engine: AsyncEngine) -> str:
    """A handed-off run that has finished and is waiting to be delivered."""
    db = Database(engine=db_engine)
    delegation_id = f"delegation_{uuid.uuid4().hex[:8]}"
    run: DelegationRunCreate = {
        "delegation_id": delegation_id,
        "task_id": f"task_{delegation_id}",
        "source_profile_id": SOURCE_PROFILE,
        "target_service_id": "target_profile",
        "interface_type": INTERFACE,
        "origin_interface_type": None,
        "conversation_id": CONVERSATION,
        "user_id": USER_ID,
        "user_name": USER_NAME,
        "source_turn_id": "turn_that_delegated",
        "subconversation_id": f"sub_{delegation_id}",
        "source_subconversation_id": None,
        "request_text": "research the options",
        "content_parts_json": [],
        "taint_state_json": None,
        "model_selection_json": None,
    }
    await db.delegation_runs.create_run(run)
    clock = SystemClock()
    await db.delegation_runs.mark_handed_off(delegation_id, clock.now())
    await db.delegation_runs.mark_completed(
        delegation_id=delegation_id,
        result_text="Three options: A, B and C.",
        result_attachment_ids=[],
        completed_at=clock.now(),
    )
    return delegation_id


def _payload(delegation_id: str) -> DelegationDeliveryPayload:
    return {"delegation_id": delegation_id, "force": False}


async def _note_content(db_engine: AsyncEngine) -> str | None:
    note = await Database(engine=db_engine).notes.get_by_title(
        NOTE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    return note.content if note else None


async def _wake_turn_roles(db_engine: AsyncEngine) -> list[str]:
    rows = await Database(engine=db_engine).fetch_all(
        select(message_history_table.c.role, message_history_table.c.turn_id)
        .where(message_history_table.c.conversation_id == CONVERSATION)
        .order_by(message_history_table.c.internal_id)
    )
    return [row["role"] for row in rows]


@pytest.mark.asyncio
async def test_a_cut_off_delivery_resumes_without_repeating_its_tools(
    db_engine: AsyncEngine,
) -> None:
    """The next delivery continues the wake turn instead of running it again.

    The first attempt saves the result to a note and is cut off before its
    reply. The next one sees the tool round it already did and only replies.
    """
    llm = _WakeTurnLLM()
    service = await _source_service(llm)
    chat_interface = RecordingChatInterface()
    worker = _worker(db_engine, service, chat_interface)
    delegation_id = await _finished_run(db_engine)

    llm.hold()
    # The handler launches the wake turn and returns while it runs.
    await worker.handle_delegation_delivery(
        _context(db_engine, service, chat_interface), _payload(delegation_id)
    )
    await asyncio.wait_for(llm.reply_requested.wait(), timeout=30)
    assert worker.background_turns.running == 1
    await worker.background_turns.cancel_all()

    assert await _note_content(db_engine) == NOTE_LINE
    assert chat_interface.sent == []
    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        delegation_id
    )
    assert run is not None
    assert run["notified_at"] is None

    llm.hold_reply.set()
    await worker.handle_delegation_delivery(
        _context(db_engine, service, chat_interface), _payload(delegation_id)
    )
    await worker.background_turns.wait_idle()

    assert await _note_content(db_engine) == NOTE_LINE
    assert [message.text for message in chat_interface.sent] == [FINAL_REPLY]
    assert await _wake_turn_roles(db_engine) == [
        "user",
        "system",
        "assistant",
        "tool",
        "assistant",
    ]
    resumed = llm.calls[-1]
    trigger_at = next(
        index
        for index, message in enumerate(resumed)
        if isinstance(message, SystemMessage)
        and "Delegated profile task completed." in str(message.content)
    )
    tool_call_at = next(
        index
        for index, message in enumerate(resumed)
        if isinstance(message, AssistantMessage) and message.tool_calls
    )
    assert trigger_at < tool_call_at, "the trigger keeps its place before the work"
    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        delegation_id
    )
    assert run is not None
    assert run["notified_at"] is not None


@pytest.mark.asyncio
async def test_a_turn_cut_off_before_its_trigger_resumes_from_its_data_row(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cut off between the data row and the trigger, the turn still runs once.

    The retry reuses the data row rather than writing a second copy of the
    result into the conversation.
    """
    llm = _WakeTurnLLM()
    service = await _source_service(llm)
    chat_interface = RecordingChatInterface()
    worker = _worker(db_engine, service, chat_interface)
    delegation_id = await _finished_run(db_engine)
    run_turn = service.handle_chat_interaction

    async def cut_off_before_the_turn_starts(**_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        service, "handle_chat_interaction", cut_off_before_the_turn_starts
    )
    await worker.handle_delegation_delivery(
        _context(db_engine, service, chat_interface), _payload(delegation_id)
    )
    await worker.background_turns.wait_idle()
    assert await _wake_turn_roles(db_engine) == ["user"]

    monkeypatch.setattr(service, "handle_chat_interaction", run_turn)
    await worker.handle_delegation_delivery(
        _context(db_engine, service, chat_interface), _payload(delegation_id)
    )
    await worker.background_turns.wait_idle()

    assert await _wake_turn_roles(db_engine) == [
        "user",
        "system",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [message.text for message in chat_interface.sent] == [FINAL_REPLY]


@pytest.mark.asyncio
async def test_a_turn_suspended_by_a_shutdown_is_resumed_through_its_lease(
    db_engine: AsyncEngine,
) -> None:
    """A rollout mid-turn hands the delivery to another process promptly.

    The shutdown suspends the wake turn and hands its lease off; the process
    that picks up the lease queues the delivery again, and that delivery
    finishes the turn without redoing its tools.
    """
    llm = _WakeTurnLLM()
    service = await _source_service(llm)
    chat_interface = RecordingChatInterface()
    db = Database(engine=db_engine)
    dying_registry = TurnLeaseRegistry()
    dying_worker = _worker(db_engine, service, chat_interface, dying_registry)
    delegation_id = await _finished_run(db_engine)
    await queue_delegation_delivery(db.tasks, delegation_id, force=False)

    llm.hold()
    await dying_worker.handle_delegation_delivery(
        _context(db_engine, service, chat_interface), _payload(delegation_id)
    )
    # The delivery task is done once its handler has launched the turn.
    await db.tasks.update_status(
        task_id=delegation_delivery_task_id(delegation_id), status="done"
    )
    await asyncio.wait_for(llm.reply_requested.wait(), timeout=30)
    await dying_registry.suspend_all(grace_seconds=0.1, teardown_seconds=5.0)
    assert dying_worker.background_turns.running == 0
    assert await dying_registry.hand_off(db) == 1

    leases = await db.tasks.get_all(task_type=TURN_RESUME_TASK_TYPE, status="pending")
    assert len(leases) == 1
    surviving_registry = TurnLeaseRegistry()
    surviving_registry.register_resumer(
        DELEGATION_WAKE_RESUMER, DelegationWakeResumer(db)
    )
    surviving_worker = _worker(db_engine, service, chat_interface, surviving_registry)
    await surviving_registry.handle_resume_task(
        _context(db_engine, service, chat_interface), leases[0]["payload"]
    )
    await db.tasks.update_status(task_id=leases[0]["task_id"], status="done")

    delivery = await db.fetch_one(
        select(tasks_table).where(
            tasks_table.c.task_id == delegation_delivery_task_id(delegation_id)
        )
    )
    assert delivery is not None
    assert delivery["status"] == "pending"
    assert delivery["payload"] == {
        "delegation_id": delegation_id,
        "force": True,
        "wake_attempt": 1,
    }

    llm.hold_reply.set()
    assert (
        await deliver_queued_delegations(
            surviving_worker, _context(db_engine, service, chat_interface)
        )
        == 1
    )

    assert await _note_content(db_engine) == NOTE_LINE
    assert [message.text for message in chat_interface.sent] == [FINAL_REPLY]
    assert await _wake_turn_roles(db_engine) == [
        "user",
        "system",
        "assistant",
        "tool",
        "assistant",
    ]
    run = await db.delegation_runs.get_by_delegation_id(delegation_id)
    assert run is not None
    assert run["notified_at"] is not None
    assert (
        await db.tasks.get_all(task_type=TURN_RESUME_TASK_TYPE, status="pending") == []
    )


@pytest.mark.asyncio
async def test_a_delivery_already_in_flight_is_left_to_finish(
    db_engine: AsyncEngine,
) -> None:
    """Queuing a run again while its delivery is pending adds no second one."""
    db = Database(engine=db_engine)
    delegation_id = await _finished_run(db_engine)

    await queue_delegation_delivery(db.tasks, delegation_id, force=False)
    await queue_delegation_delivery(db.tasks, delegation_id, force=True)

    rows = await db.tasks.get_all(task_type=DELEGATION_DELIVERY_TASK_TYPE)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"delegation_id": delegation_id, "force": False}


@pytest.mark.asyncio
async def test_a_finished_delivery_is_queued_again_when_the_run_still_needs_one(
    db_engine: AsyncEngine,
) -> None:
    """A delivery that ended without notifying does not block the next one."""
    db = Database(engine=db_engine)
    delegation_id = await _finished_run(db_engine)
    task_id = delegation_delivery_task_id(delegation_id)
    await queue_delegation_delivery(db.tasks, delegation_id, force=False)
    await db.tasks.update_status(task_id=task_id, status="failed")

    await queue_delegation_delivery(db.tasks, delegation_id, force=True)

    rows = await db.tasks.get_all(task_type=DELEGATION_DELIVERY_TASK_TYPE)
    assert [(row["status"], row["payload"]) for row in rows] == [
        ("pending", {"delegation_id": delegation_id, "force": True})
    ]


@pytest.mark.asyncio
async def test_a_delivery_finishing_while_it_is_queued_again_is_replaced(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running delivery can finish between the delete and the insert.

    Its row then still blocks the insert although nothing is delivering any
    more, so queuing has to look again rather than take the clash as a live
    delivery.
    """
    db = Database(engine=db_engine)
    delegation_id = await _finished_run(db_engine)
    task_id = delegation_delivery_task_id(delegation_id)
    await queue_delegation_delivery(db.tasks, delegation_id, force=False)
    assert await db.tasks.dequeue(
        worker_id="finishing-worker",
        task_types=[DELEGATION_DELIVERY_TASK_TYPE],
        current_time=SystemClock().now(),
    )

    delete_finished = TasksRepository.delete_finished
    finished = False

    async def finish_after_first_delete(self: TasksRepository, task_id_: str) -> bool:
        nonlocal finished
        deleted = await delete_finished(self, task_id_)
        if not finished:
            finished = True
            await db.tasks.update_status(task_id=task_id, status="done")
        return deleted

    monkeypatch.setattr(TasksRepository, "delete_finished", finish_after_first_delete)

    await queue_delegation_delivery(db.tasks, delegation_id, force=True)

    rows = await db.tasks.get_all(task_type=DELEGATION_DELIVERY_TASK_TYPE)
    assert [(row["status"], row["payload"]) for row in rows] == [
        ("pending", {"delegation_id": delegation_id, "force": True})
    ]


@pytest.mark.asyncio
async def test_a_turn_out_of_attempts_still_gets_its_final_reply_delivered(
    db_engine: AsyncEngine,
) -> None:
    """A turn that has used up its resumes still gets its final reply sent.

    It is closed with an interrupted marker, and sending that marker is a
    delivery of its own.
    """
    db = Database(engine=db_engine)
    delegation_id = await _finished_run(db_engine)
    task_id = delegation_delivery_task_id(delegation_id)
    await queue_delegation_delivery(db.tasks, delegation_id, force=False)
    await db.tasks.update_status(task_id=task_id, status="done")

    await DelegationWakeResumer(db).deliver_pending_reply(
        TurnResumePayload(
            resumer=DELEGATION_WAKE_RESUMER,
            interface_type=INTERFACE,
            conversation_id=CONVERSATION,
            turn_id="wake_turn",
            user_id=USER_ID,
            user_name=USER_NAME,
            processing_profile_id=SOURCE_PROFILE,
            delegation_id=delegation_id,
            attempt=MAX_RESUME_ATTEMPTS,
        )
    )

    rows = await db.tasks.get_all(task_type=DELEGATION_DELIVERY_TASK_TYPE)
    assert [(row["status"], row["retry_count"], row["payload"]) for row in rows] == [
        ("pending", 0, {"delegation_id": delegation_id, "force": True})
    ]
