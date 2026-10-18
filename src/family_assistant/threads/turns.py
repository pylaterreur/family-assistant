"""Running a thread's first turn where its user can watch it.

The task worker owns a thread run's lifecycle -- admission, the durable queue,
failure and completion -- but the turn itself belongs to a conversation the user
reads. A surface that can stream a turn live supplies a runner, and the worker
hands the turn to it, so the thread shows the turn as it happens and accepts
Stop and steering like any turn the user started. Without a runner the worker
runs the turn itself, out of sight until it finishes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.processing import ProcessingService
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunDict,
    )


@dataclass(frozen=True, slots=True)
class ThreadTurnOutcome:
    """How a thread's first turn ended."""

    succeeded: bool
    reply_text: str
    error: str | None


class ThreadTurnRunner(Protocol):
    """Runs a thread run's first turn live in its conversation."""

    def handles(self, run: DelegationRunDict) -> bool:
        """Whether this runner serves the surface ``run``'s thread lives on."""
        ...

    async def run_first_turn(
        self,
        run: DelegationRunDict,
        *,
        target: ProcessingService,
        model_selection: ResolvedModelSelection,
        tool_call_review_trigger: TriggerReviewInput | None,
        claim: Callable[[], Awaitable[bool]],
    ) -> ThreadTurnOutcome | None:
        """Run the turn, or return None if it could not start.

        ``tool_call_review_trigger`` is the request the run was started for, as
        the tool-call reviewer judges the turn's actions against it.

        ``claim`` marks the run running; it is called once the turn is
        registered where its user can see it, and the turn runs only if it
        succeeds. None means either the conversation already had a turn in
        progress or the claim failed -- the run was no longer queued.
        """
        ...
