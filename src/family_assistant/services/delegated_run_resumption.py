"""Resume a delegated run's turn after the process running it went away.

A delegated run's turn runs on the background turn runner under a turn lease.
When the lease comes due, this resumer queues a ``delegated_profile_run`` task
that names the interrupted turn, and the worker that claims it decides from the
run's own status whether anything is left to do and, if so, continues the turn
from the rows it already persisted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, TypedDict

from family_assistant.storage.tasks import TaskAlreadyExistsError, TaskPriority
from family_assistant.tools.services import DELEGATED_PROFILE_RUN_TASK_TYPE

if TYPE_CHECKING:
    from family_assistant.services.turn_resumption import (
        TurnLeaseRegistry,
        TurnResumePayload,
    )
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

DELEGATED_RUN_RESUMER = "delegated_run"


class ResumedDelegatedTurn(TypedDict):
    """The interrupted turn a ``delegated_profile_run`` task continues."""

    turn_id: str
    kind: Literal["request", "continuation"]
    # How many times the turn has been relaunched, counting this one.
    attempt: int


class DelegatedRunResumer:
    """Queues the continuation of a delegated run's interrupted turn."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def resume_interrupted(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> None:
        del registry  # The worker arms the resumed turn's lease itself.
        if payload.delegation_id is None or payload.delegated_turn is None:
            raise ValueError(
                f"Delegated turn {payload.turn_id} has no delegation id or turn kind"
            )
        attempt = payload.attempt + 1
        resumed: ResumedDelegatedTurn = {
            "turn_id": payload.turn_id,
            "kind": payload.delegated_turn,
            "attempt": attempt,
        }
        try:
            await self._database.tasks.enqueue(
                task_id=(
                    f"{DELEGATED_PROFILE_RUN_TASK_TYPE}_resume:"
                    f"{payload.delegation_id}:{payload.turn_id}:{attempt}"
                ),
                task_type=DELEGATED_PROFILE_RUN_TASK_TYPE,
                payload={
                    "delegation_id": payload.delegation_id,
                    "interface_type": payload.interface_type,
                    "conversation_id": payload.conversation_id,
                    "user_name": payload.user_name,
                    "resume": resumed,
                },
                priority=TaskPriority.INTERACTIVE,
            )
        except TaskAlreadyExistsError:
            logger.info(
                "Resumption of delegated turn %s (attempt %d) is already queued.",
                payload.turn_id,
                attempt,
            )
            return
        logger.info(
            "Queued resumption of delegated turn %s of %s (attempt %d).",
            payload.turn_id,
            payload.delegation_id,
            attempt,
        )
