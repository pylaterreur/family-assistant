"""Delegated turns run on the background turn runner, leased and resumable.

The handler launches a local run's turn and returns; the turn holds a lease that
resumes it if its process goes away, and the runner bounds how many run at once.
See docs/design/background-turns-off-the-task-worker.md.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageAttachmentMetadata,
    UserMessage,
)
from family_assistant.processing.background_turns import BackgroundTurnRunner
from family_assistant.processing.types import (
    ChatInteractionResult,
    ChatInteractionStatus,
)
from family_assistant.services.delegated_run_resumption import (
    DELEGATED_RUN_RESUMER,
    DelegatedRunResumer,
)
from family_assistant.services.turn_resumption import (
    MAX_RESUME_ATTEMPTS,
    TURN_RESUME_TASK_TYPE,
    TurnLeaseRegistry,
)
from family_assistant.storage.database import Database
from family_assistant.task_worker import (
    DelegatedProfileRunPayload,
    TaskWorker,
)
from family_assistant.tools.services import DELEGATED_PROFILE_RUN_TASK_TYPE
from family_assistant.utils.clock import SystemClock
from tests.functional.automations.test_async_delegation import (
    FakeDelegatableService,
    FakeDelegationCall,
    _create_run,
    _payload,
    _source_processing_service,
    _tool_context,
)
from tests.helpers import (
    deliver_queued_delegations,
    run_delegated_profile_run,
    wait_for_condition,
)
from tests.mocks.recording_chat_interface import ScriptedChatInterface

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing.service import ProcessingService


class _BlockingService(FakeDelegatableService):
    """A target whose turn runs until it is cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.calls.append(cast("FakeDelegationCall", kwargs))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SuspendedOnceService(FakeDelegatableService):
    """Saves its prompt and runs until suspended, then finishes when resumed."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.calls.append(cast("FakeDelegationCall", kwargs))
        if len(self.calls) > 1:
            return ChatInteractionResult(
                status=ChatInteractionStatus.SUCCESS, text_reply="resumed and done"
            )
        db: Database = kwargs["db_context"]
        await db.message_history.add_message(
            UserMessage.from_trusted_user(content="do the thing"),
            interface_type=kwargs["interface_type"],
            conversation_id=kwargs["conversation_id"],
            timestamp=SystemClock().now(),
            turn_id=kwargs["turn_id"],
            subconversation_id=kwargs["subconversation_id"],
            user_id="async-delegation-user",
        )
        self.started.set()
        provider = kwargs["mid_turn_input_provider"]
        await wait_for_condition(provider.should_interrupt, timeout=10.0, interval=0.01)
        raise asyncio.CancelledError("Turn interrupted by user")


def _worker(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    chat_interface: ChatInterface,
    *,
    runner: BackgroundTurnRunner,
) -> TaskWorker:
    return TaskWorker(
        processing_service=processing_service,
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MockEmbeddingGenerator(),
        engine=db_engine,
        turn_lease_registry=runner.lease_registry,
        background_turns=runner,
    )


async def _start_run(db: Database, delegation_id: str) -> None:
    await _create_run(db, delegation_id=delegation_id)
    await db.delegation_runs.mark_handed_off(delegation_id, SystemClock().now())


@pytest.mark.asyncio
async def test_the_handler_returns_while_the_turn_runs_and_full_runners_defer(
    db_engine: AsyncEngine,
) -> None:
    """A run's turn outlives its task; a run with no room waits its turn."""
    target = _BlockingService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    runner = BackgroundTurnRunner(max_concurrent=1)
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)
    context = _tool_context(db, processing_service, chat_interface)
    await _start_run(db, "delegation_first")
    await _start_run(db, "delegation_second")

    try:
        await worker.handle_delegated_profile_run(context, _payload("delegation_first"))
        await asyncio.wait_for(target.started.wait(), timeout=10)
        assert runner.running == 1
        first = await db.delegation_runs.get_by_delegation_id("delegation_first")
        assert first is not None
        assert first["status"] == "running"

        await worker.handle_delegated_profile_run(
            context, _payload("delegation_second")
        )

        second = await db.delegation_runs.get_by_delegation_id("delegation_second")
        assert second is not None
        assert second["status"] == "queued"
        assert len(target.calls) == 1
        deferred = [
            task
            for task in await db.tasks.get_all(
                status="pending", task_type=DELEGATED_PROFILE_RUN_TASK_TYPE
            )
            if (task["payload"] or {}).get("delegation_id") == "delegation_second"
        ]
        assert len(deferred) == 1
        assert deferred[0]["scheduled_at"] is not None
    finally:
        await runner.cancel_all()


@pytest.mark.asyncio
async def test_a_turn_cut_off_by_a_restart_is_resumed_not_failed(
    db_engine: AsyncEngine,
) -> None:
    """The lease hands the turn on, and the next process continues it in place."""
    target = _SuspendedOnceService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    await _start_run(db, "delegation_resumed")

    # The process that is shut down partway through the turn.
    dying_registry = TurnLeaseRegistry()
    dying_runner = BackgroundTurnRunner(lease_registry=dying_registry)
    dying_worker = _worker(
        db_engine, processing_service, chat_interface, runner=dying_runner
    )
    context = _tool_context(db, processing_service, chat_interface)
    await dying_worker.handle_delegated_profile_run(
        context, _payload("delegation_resumed")
    )
    await asyncio.wait_for(target.started.wait(), timeout=10)
    await dying_registry.suspend_all(grace_seconds=10)
    await dying_runner.wait_idle()
    assert await dying_registry.hand_off(db) == 1
    run = await db.delegation_runs.get_by_delegation_id("delegation_resumed")
    assert run is not None
    assert run["status"] == "running"

    # The process that picks the lease up.
    registry = TurnLeaseRegistry()
    registry.register_self_deciding_resumer(
        DELEGATED_RUN_RESUMER, DelegatedRunResumer(db)
    )
    runner = BackgroundTurnRunner(lease_registry=registry)
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)
    [lease] = await db.tasks.get_all(status="pending", task_type=TURN_RESUME_TASK_TYPE)
    await registry.handle_resume_task(context, lease["payload"])
    [resumption] = [
        task
        for task in await db.tasks.get_all(
            status="pending", task_type=DELEGATED_PROFILE_RUN_TASK_TYPE
        )
        if "resume" in (task["payload"] or {})
    ]
    await run_delegated_profile_run(
        worker, context, cast("DelegatedProfileRunPayload", resumption["payload"])
    )
    await deliver_queued_delegations(worker, context)

    assert len(target.calls) == 2
    first_call, resumed_call = target.calls[0], target.calls[1]
    assert first_call["resume"] is False
    assert resumed_call["resume"] is True
    assert resumed_call["turn_id"] == first_call["turn_id"]
    run = await db.delegation_runs.get_by_delegation_id("delegation_resumed")
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] == "resumed and done"


@pytest.mark.asyncio
async def test_a_turn_interrupted_too_often_is_failed_saying_so(
    db_engine: AsyncEngine,
) -> None:
    """Resumption is bounded, so a turn that keeps taking its process down ends."""
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    await _start_run(db, "delegation_crashy")
    await db.delegation_runs.mark_running("delegation_crashy", SystemClock().now())
    runner = BackgroundTurnRunner()
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)
    context = _tool_context(db, processing_service, chat_interface)
    payload = _payload("delegation_crashy")
    payload["resume"] = {
        "turn_id": "turn_crashy",
        "kind": "request",
        "attempt": MAX_RESUME_ATTEMPTS + 1,
    }

    await run_delegated_profile_run(worker, context, payload)

    assert target.calls == []
    run = await db.delegation_runs.get_by_delegation_id("delegation_crashy")
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] is not None
    assert "interrupted by a restart" in run["error"]


@pytest.mark.asyncio
async def test_a_stray_retry_of_a_running_run_leaves_it_to_its_lease(
    db_engine: AsyncEngine,
) -> None:
    """Only a resumption continues a running run; a retry neither reruns nor fails it."""
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    await _start_run(db, "delegation_owned")
    await db.delegation_runs.mark_running("delegation_owned", SystemClock().now())
    runner = BackgroundTurnRunner()
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)

    await run_delegated_profile_run(
        worker,
        _tool_context(db, processing_service, chat_interface),
        _payload("delegation_owned"),
    )

    assert target.calls == []
    run = await db.delegation_runs.get_by_delegation_id("delegation_owned")
    assert run is not None
    assert run["status"] == "running"


@pytest.mark.asyncio
async def test_a_continuation_cut_off_before_recording_its_inputs_starts_afresh(
    db_engine: AsyncEngine,
) -> None:
    """With nothing saved under its turn, a continuation records its inputs again."""
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    await _start_run(db, "delegation_continued")
    await db.delegation_runs.mark_running("delegation_continued", SystemClock().now())
    runner = BackgroundTurnRunner()
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)
    payload = _payload("delegation_continued")
    payload["continuation"] = True
    payload["resume"] = {
        "turn_id": "turn_unrecorded",
        "kind": "continuation",
        "attempt": 1,
    }

    await run_delegated_profile_run(
        worker, _tool_context(db, processing_service, chat_interface), payload
    )

    assert [call["resume"] for call in target.calls] == [False]
    assert target.calls[0]["turn_id"] == "turn_unrecorded"


@pytest.mark.asyncio
async def test_a_turn_finished_before_a_restart_settles_with_its_attachments(
    db_engine: AsyncEngine,
) -> None:
    """Settling from the saved reply keeps what the turn produced, not just its text."""
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = ScriptedChatInterface(otherwise="external_message_id")
    db = Database(engine=db_engine)
    await _start_run(db, "delegation_finished")
    await db.delegation_runs.mark_running("delegation_finished", SystemClock().now())
    payload = _payload("delegation_finished")
    chart: list[MessageAttachmentMetadata] = [
        {"type": "tool_result", "attachment_id": "chart-attachment"}
    ]
    for message, attachments in (
        (UserMessage.from_trusted_user(content="draw a chart"), None),
        (AssistantMessage(content="Here is the chart."), chart),
    ):
        await db.message_history.add_message(
            message,
            interface_type=payload["interface_type"],
            conversation_id=payload["conversation_id"],
            timestamp=SystemClock().now(),
            turn_id="turn_finished",
            attachments=attachments,
        )
    runner = BackgroundTurnRunner()
    worker = _worker(db_engine, processing_service, chat_interface, runner=runner)
    payload["resume"] = {"turn_id": "turn_finished", "kind": "request", "attempt": 1}

    await run_delegated_profile_run(
        worker, _tool_context(db, processing_service, chat_interface), payload
    )

    assert target.calls == []
    run = await db.delegation_runs.get_by_delegation_id("delegation_finished")
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] == "Here is the chart."
    assert run["result_attachment_ids_json"] == ["chart-attachment"]
