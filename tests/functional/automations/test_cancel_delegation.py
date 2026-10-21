"""Tests for cancel_delegation: stopping a delegation nobody wants any more.

Each test drives the real tool and, where it matters, the real worker handler
that would otherwise have carried the run on, against the fakes the other
delegation tests use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from family_assistant.processing import RemoteDisposition
from family_assistant.storage.database import Database
from family_assistant.tools.services import (
    cancel_delegation_tool,
    list_delegations_tool,
)
from family_assistant.utils.clock import SystemClock
from tests.functional.automations.test_async_delegation import (
    TEST_CONVERSATION_ID,
    TEST_INTERFACE_TYPE,
    TEST_USER_NAME,
    FakeDelegatableService,
    FakePollableService,
    _build_worker,
    _create_run,
    _payload,
    _source_processing_service,
    _tool_context,
)
from tests.functional.automations.test_delegation_reconciliation import (
    REMOTE_TASK_ID,
    FakeObservableService,
    _observation,
    _reconcile_payload,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing.service import ProcessingService
    from family_assistant.task_worker import DelegationPollPayload


class FailingCancelService(FakePollableService):
    """A remote target whose cancel request does not get through."""

    async def cancel_async(self, remote_task_id: str) -> None:
        raise ConnectionError(f"could not reach the provider for {remote_task_id}")


def _poll_payload(delegation_id: str) -> DelegationPollPayload:
    return {
        "delegation_id": delegation_id,
        "interface_type": TEST_INTERFACE_TYPE,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
    }


async def _remote_run(db_context: Database, delegation_id: str) -> None:
    await _create_run(db_context, delegation_id=delegation_id)
    await db_context.delegation_runs.mark_handed_off(delegation_id, SystemClock().now())
    await db_context.delegation_runs.mark_awaiting_remote(
        delegation_id,
        remote_task_id=REMOTE_TASK_ID,
        remote_context_id=None,
        started_at=SystemClock().now(),
    )


def _processing_service(target: FakePollableService) -> ProcessingService:
    return _source_processing_service(cast("FakeDelegatableService", target))


@pytest.mark.asyncio
async def test_cancelling_a_remote_run_stops_it_and_drops_its_result(
    db_engine: AsyncEngine,
) -> None:
    """The provider is asked to stop, and a later completion is never delivered.

    The duplicate coder job that prompted the tool finished anyway after it
    was no longer wanted; its result must not come back as a late recovery.
    """
    target = FakeObservableService([
        _observation(RemoteDisposition.COMPLETED, output_text="unwanted work")
    ])
    processing_service = _processing_service(target)
    chat_interface = AsyncMock()
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_duplicate")
    context = _tool_context(db_context, processing_service, chat_interface)

    result = await cancel_delegation_tool(context, "delegation_duplicate")

    assert result.text is not None
    assert "Cancelled delegation 'delegation_duplicate'" in result.text
    assert "asked to stop" in result.text
    assert target.cancelled == [REMOTE_TASK_ID]
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_duplicate")
    assert run is not None
    assert run["status"] == "failed"
    assert run["local_failure_kind"] == "cancelled"
    assert run["cancel_requested_at"] is not None
    assert run["notified_at"] is not None

    worker = _build_worker(db_engine, processing_service, chat_interface)
    await worker.handle_delegation_poll(context, _poll_payload("delegation_duplicate"))
    await worker.handle_delegation_reconcile(
        context, _reconcile_payload("delegation_duplicate")
    )

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_duplicate")
    assert run is not None
    assert run["status"] == "failed"
    assert run["late_recovered_at"] is None
    assert target.observe_calls == []
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_run_is_listed_as_cancelled_and_no_longer_pending(
    db_engine: AsyncEngine,
) -> None:
    """The model can tell a cancelled run from one that failed on its own."""
    target = FakePollableService()
    processing_service = _processing_service(target)
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_listed")
    context = _tool_context(db_context, processing_service)

    await cancel_delegation_tool(context, "delegation_listed")
    listing = await list_delegations_tool(context)

    assert isinstance(listing.data, list)
    [summary] = listing.data
    assert summary["status"] == "failed"
    assert summary["cancelled"] is True
    assert (
        await db_context.delegation_runs.list_pending_for_conversation(
            conversation_id=TEST_CONVERSATION_ID
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_queued_run_is_never_started_once_cancelled(
    db_engine: AsyncEngine,
) -> None:
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = AsyncMock()
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_queued")
    await db_context.delegation_runs.mark_handed_off(
        "delegation_queued", SystemClock().now()
    )
    context = _tool_context(db_context, processing_service, chat_interface)

    result = await cancel_delegation_tool(context, "delegation_queued")

    assert result.text is not None
    assert "Cancelled delegation 'delegation_queued'" in result.text
    worker = _build_worker(db_engine, processing_service, chat_interface)
    await worker.handle_delegated_profile_run(context, _payload("delegation_queued"))
    assert target.calls == []
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_running_here_is_not_cancelled(db_engine: AsyncEngine) -> None:
    """Nothing can interrupt it, so the run is left to finish and report."""
    processing_service = _source_processing_service(FakeDelegatableService())
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_running")
    await db_context.delegation_runs.mark_running(
        "delegation_running", SystemClock().now()
    )

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_running"
    )

    assert result.text is not None
    assert result.text.startswith("Error:")
    assert "cannot be stopped partway" in result.text
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_running")
    assert run is not None
    assert run["status"] == "running"


@pytest.mark.asyncio
async def test_a_finished_run_has_nothing_to_cancel(db_engine: AsyncEngine) -> None:
    processing_service = _source_processing_service(FakeDelegatableService())
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_done")
    await db_context.delegation_runs.mark_completed(
        delegation_id="delegation_done",
        result_text="done",
        result_attachment_ids=[],
        completed_at=SystemClock().now(),
    )

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_done"
    )

    assert result.text == (
        "Delegation 'delegation_done' has already completed; there is nothing to cancel."
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_done")
    assert run is not None
    assert run["status"] == "completed"


@pytest.mark.asyncio
async def test_another_conversations_run_cannot_be_cancelled(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService()
    processing_service = _processing_service(target)
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_elsewhere")

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service, interface_type="telegram"),
        "delegation_elsewhere",
    )

    assert result.text == (
        "Error: Delegation 'delegation_elsewhere' not found in this conversation."
    )
    assert target.cancelled == []
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_elsewhere")
    assert run is not None
    assert run["status"] == "awaiting_remote"


@pytest.mark.asyncio
async def test_a_failed_remote_cancel_still_cancels_and_says_the_agent_may_continue(
    db_engine: AsyncEngine,
) -> None:
    target = FailingCancelService()
    processing_service = _processing_service(target)
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_unreachable")

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_unreachable"
    )

    assert result.text is not None
    assert "Cancelled delegation 'delegation_unreachable'" in result.text
    assert "may run to the end" in result.text
    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_unreachable"
    )
    assert run is not None
    assert run["status"] == "failed"
    assert run["local_failure_kind"] == "cancelled"
    assert run["cancel_requested_at"] is None
