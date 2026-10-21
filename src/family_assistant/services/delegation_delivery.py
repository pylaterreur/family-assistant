"""Deliver a finished delegation's result as a task of its own.

Delivering a result can mean waking the profile that delegated, which runs a
whole turn. That turn gets its own task and handler budget rather than whatever
is left of the task that noticed the run had finished: a poll, a reconcile, or
the hourly cleanup. Every one of them queues delivery through here.

There is one delivery task per delegation, under a deterministic id. A
delivery already pending or running owns the work, so queuing again leaves it
alone; a finished one is replaced, because a run that is queued for delivery
again was not delivered by it.

A wake turn holds a turn lease (see ``turn_resumption``). When its process
goes away mid-turn the lease comes due, and :class:`DelegationWakeResumer`
hands the delivery task back to the queue, whose next run resumes the turn
from the rows it already persisted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypedDict

from family_assistant.storage.tasks import TaskAlreadyExistsError, TaskPriority
from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.services.turn_resumption import (
        TurnLeaseRegistry,
        TurnResumePayload,
    )
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.tasks import TasksRepository

logger = logging.getLogger(__name__)

DELEGATION_DELIVERY_TASK_TYPE = "delegation_delivery"
DELEGATION_WAKE_RESUMER = "delegation_wake"

# The wake turn's budget. Under the stale-claim cutoff (15 minutes), so a live
# delivery is never reclaimed out from under itself into a second, concurrent
# wake turn. A delivery cut off by it is retried, and the retry resumes the turn.
DELEGATION_DELIVERY_TIMEOUT_SECONDS = 600.0


class DelegationDeliveryPayload(TypedDict):
    """Payload for delegation_delivery tasks."""

    delegation_id: str
    # Deliver even if the caller never handed the run off (see
    # ``TaskWorker._notify_delegation_if_needed``).
    force: bool


def delegation_delivery_task_id(delegation_id: str) -> str:
    return f"{DELEGATION_DELIVERY_TASK_TYPE}:{delegation_id}"


async def queue_delegation_delivery(
    tasks: TasksRepository, delegation_id: str, *, force: bool
) -> None:
    """Make sure a delivery of ``delegation_id`` is pending or running."""
    task_id = delegation_delivery_task_id(delegation_id)
    payload: DelegationDeliveryPayload = {
        "delegation_id": delegation_id,
        "force": force,
    }
    await tasks.delete_finished(task_id)
    if await _enqueue_delivery(tasks, task_id, payload):
        return
    # The row in the way may have finished between the delete and the insert,
    # in which case nothing is left to deliver. Once more settles it: a row
    # that is still there now is active.
    if await tasks.delete_finished(task_id) and await _enqueue_delivery(
        tasks, task_id, payload
    ):
        return
    logger.info(
        "Delivery of delegation %s is already queued or running.", delegation_id
    )


async def _enqueue_delivery(
    tasks: TasksRepository, task_id: str, payload: DelegationDeliveryPayload
) -> bool:
    try:
        await tasks.enqueue(
            task_id=task_id,
            task_type=DELEGATION_DELIVERY_TASK_TYPE,
            payload=payload,
            priority=TaskPriority.INTERACTIVE,
        )
    except TaskAlreadyExistsError:
        return False
    return True


async def redeliver_now(
    tasks: TasksRepository, delegation_id: str, *, now: datetime, claimed_by: str
) -> None:
    """Run the delegation's delivery as soon as a worker is free.

    ``claimed_by``'s claim is released: this is called once the process that
    holds it stopped renewing its turn lease. A finished delivery is queued
    again, forced -- a wake turn exists only because its delivery was due. A
    delivery another worker has claimed since is left to it.
    """
    task_id = delegation_delivery_task_id(delegation_id)
    if await tasks.release_claim(task_id, now, locked_by=claimed_by):
        return
    if await tasks.reschedule_pending([task_id], now):
        return
    await queue_delegation_delivery(tasks, delegation_id, force=True)


class DelegationWakeResumer:
    """Resumes interrupted delegation wake turns through their delivery task."""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self._database = database
        self._clock = clock or SystemClock()

    async def resume(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> bool:
        del registry  # The delivery task arms the resumed turn's lease itself.
        delegation_id, claimed_by = self._claim(payload)
        await redeliver_now(
            self._database.tasks,
            delegation_id,
            now=self._clock.now(),
            claimed_by=claimed_by,
        )
        return True

    async def deliver_pending_reply(self, payload: TurnResumePayload) -> None:
        delegation_id, claimed_by = self._claim(payload)
        tasks = self._database.tasks
        await tasks.release_claim(
            delegation_delivery_task_id(delegation_id),
            self._clock.now(),
            locked_by=claimed_by,
        )
        # Releasing the last attempt fails the row, but the turn has its final
        # reply and only sending it is left, so a fresh delivery takes over.
        await queue_delegation_delivery(tasks, delegation_id, force=True)

    @staticmethod
    def _claim(payload: TurnResumePayload) -> tuple[str, str]:
        if payload.delegation_id is None or payload.claimed_by is None:
            raise ValueError(
                f"Delegation wake turn {payload.turn_id} has no delegation id "
                "or claiming worker"
            )
        return payload.delegation_id, payload.claimed_by
