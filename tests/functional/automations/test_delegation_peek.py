"""Peeking at a delegation still in progress through ``get_delegation_status``.

Drives the real tool against a fake target that can report progress, so what
is pinned is the tool's side: it asks only a run still going, says why when it
cannot, and grades what it returns as the delegate's output. What a real agent
reports is covered by the replayed integration test of the shipped ``coder``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.processing import DelegationTransientError
from family_assistant.security.taint import InMemoryTurnTaintTracker, TurnTaintState
from family_assistant.storage.database import Database
from family_assistant.tools.services import get_delegation_status_tool
from family_assistant.utils.clock import SystemClock
from tests.functional.automations.test_async_delegation import (
    FakeDelegatableService,
    FakePollableService,
    _create_run,
    _source_processing_service,
    _tool_context,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

REMOTE_TASK_ID = "inter-peek"
_ACCOUNT = "Remote status: in_progress. Steps so far: 2.\n- ran: python build.py"


class FakePeekableService(FakePollableService):
    """A pollable target that reports what a run has done so far."""

    def __init__(self, account: str | BaseException = _ACCOUNT) -> None:
        super().__init__()
        self._account = account
        self.peeked: list[str] = []

    async def peek_async(self, remote_task_id: str) -> str:
        self.peeked.append(remote_task_id)
        if isinstance(self._account, BaseException):
            raise self._account
        return self._account


async def _awaiting_run(db_context: Database, delegation_id: str) -> None:
    await _create_run(db_context, delegation_id=delegation_id)
    await db_context.delegation_runs.mark_awaiting_remote(
        delegation_id,
        remote_task_id=REMOTE_TASK_ID,
        remote_context_id=None,
        started_at=SystemClock().now(),
    )


def _delegate_sources(tracker: InMemoryTurnTaintTracker) -> set[str]:
    return {
        source.source_id
        for source in tracker.snapshot().sources
        if source.source_id and source.source_id.startswith("delegate_to_service:")
    }


async def _status(
    db_engine: AsyncEngine,
    target: FakePollableService,
    delegation_id: str,
    *,
    peek: bool,
) -> tuple[str, InMemoryTurnTaintTracker]:
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    tracker = InMemoryTurnTaintTracker(TurnTaintState.empty())
    result = await get_delegation_status_tool(
        _tool_context(
            Database(engine=db_engine), processing_service, taint_tracker=tracker
        ),
        delegation_id,
        peek=peek,
    )
    return result.text or "", tracker


@pytest.mark.asyncio
async def test_a_peek_reports_a_running_delegations_progress(
    db_engine: AsyncEngine,
) -> None:
    """The account is the delegate's own output, and is graded as such."""
    target = FakePeekableService()
    await _awaiting_run(Database(engine=db_engine), "delegation_peeked")

    text, tracker = await _status(db_engine, target, "delegation_peeked", peek=True)

    assert target.peeked == [REMOTE_TASK_ID]
    assert "Live progress:" in text
    assert "ran: python build.py" in text
    assert _delegate_sources(tracker) == {"delegate_to_service:target_profile"}


@pytest.mark.asyncio
async def test_a_status_check_without_a_peek_does_not_look(
    db_engine: AsyncEngine,
) -> None:
    target = FakePeekableService()
    await _awaiting_run(Database(engine=db_engine), "delegation_unpeeked")

    text, tracker = await _status(db_engine, target, "delegation_unpeeked", peek=False)

    assert target.peeked == []
    assert "Live progress:" not in text
    assert _delegate_sources(tracker) == set()


@pytest.mark.asyncio
async def test_a_finished_delegation_is_not_peeked_at(
    db_engine: AsyncEngine,
) -> None:
    target = FakePeekableService()
    db_context = Database(engine=db_engine)
    await _awaiting_run(db_context, "delegation_done")
    await db_context.delegation_runs.mark_completed(
        delegation_id="delegation_done",
        result_text="all done",
        result_attachment_ids=[],
        completed_at=SystemClock().now(),
    )

    text, _ = await _status(db_engine, target, "delegation_done", peek=True)

    assert target.peeked == []
    assert "Nothing to peek at: the delegation is completed." in text


@pytest.mark.asyncio
async def test_a_target_that_cannot_report_progress_says_so(
    db_engine: AsyncEngine,
) -> None:
    await _awaiting_run(Database(engine=db_engine), "delegation_opaque")

    text, _ = await _status(
        db_engine, FakePollableService(), "delegation_opaque", peek=True
    )

    assert "does not report progress while it runs" in text


@pytest.mark.asyncio
async def test_a_failed_peek_still_returns_the_status(
    db_engine: AsyncEngine,
) -> None:
    """A peek is a nicety: failing to look must not hide the run's status."""
    target = FakePeekableService(DelegationTransientError("connection reset"))
    await _awaiting_run(Database(engine=db_engine), "delegation_blip")

    text, tracker = await _status(db_engine, target, "delegation_blip", peek=True)

    assert '"status": "awaiting_remote"' in text
    assert "Could not look at the run just now: connection reset" in text
    assert _delegate_sources(tracker) == set()
