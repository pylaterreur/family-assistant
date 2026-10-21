"""Tests for deferred confirmation callback classification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import pytest

from family_assistant.security.taint import TurnTaintState
from family_assistant.services.deferred_tool_confirmation import (
    DeferredConfirmationCallbackAdapter,
)
from family_assistant.storage.database import Database
from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolArguments


@pytest.fixture
def execution_context(db_engine: AsyncEngine) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="api",
        conversation_id="conversation",
        user_name="tester",
        turn_id="turn",
        db_context=Database(engine=db_engine),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


class _OutcomeCallback:
    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome
        self.received_contexts: list[ToolExecutionContext] = []

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        del (
            interface_type,
            conversation_id,
            turn_id,
            tool_name,
            call_id,
            tool_args,
            timeout_seconds,
        )
        self.received_contexts.append(context)
        return self.outcome


async def _invoke_adapter(
    outcome: ConfirmationOutcome, context: ToolExecutionContext
) -> ConfirmationOutcome:
    callback = _OutcomeCallback(outcome)
    adapter = DeferredConfirmationCallbackAdapter(callback)
    result = await adapter(
        "api",
        "conversation",
        "turn",
        "reviewed_tool",
        "call",
        {},
        60,
        context,
    )
    assert len(callback.received_contexts) == 1
    assert callback.received_contexts[0] is context
    return result


async def test_deferred_adapter_marks_completed_placeholder_not_attempted(
    execution_context: ToolExecutionContext,
) -> None:
    taint_metadata = TurnTaintState.empty().to_metadata()
    result = await _invoke_adapter(
        ConfirmationOutcome(
            kind="completed",
            result="Approval request queued; the tool has not run.",
            taint_metadata=taint_metadata,
        ),
        execution_context,
    )

    assert result == ConfirmationOutcome(
        kind="completed",
        result="Approval request queued; the tool has not run.",
        action_attempted=False,
        taint_metadata=taint_metadata,
    )


@pytest.mark.parametrize(
    "kind",
    ["approved", "rejected", "timed_out", "cancelled", "failed"],
)
async def test_deferred_adapter_preserves_noncompleted_outcomes(
    kind: Literal["approved", "rejected", "timed_out", "cancelled", "failed"],
    execution_context: ToolExecutionContext,
) -> None:
    outcome = ConfirmationOutcome(kind=kind, result=f"{kind} result")

    result = await _invoke_adapter(outcome, execution_context)

    assert result is outcome
