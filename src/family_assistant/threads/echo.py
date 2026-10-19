"""Echoing a finished thread's outcome into its Home.

When a thread's assistant closes the job with ``finish_thread``, the message it
ends that turn with is the outcome, so the echo is written when the turn ends,
not when the tool runs: every turn in a conversation ends in the processing
service, whichever surface or task started it, and that is where this is
called. The echo is a copy through the boundary chokepoint, not a model turn,
so it never wakes Home's assistant; open clients are told to refresh instead.
See docs/design/one-thread-chat-ux.md, "State".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from family_assistant.threads.boundary import (
    BoundaryDestination,
    ThreadEcho,
    copy_across_boundary,
)

if TYPE_CHECKING:
    from family_assistant.processing.types import ProcessingServiceConfig
    from family_assistant.storage.database import (
        DatabaseExecutor,
        DatabaseTransaction,
    )

logger = logging.getLogger(__name__)


class HomeActivityPublisher(Protocol):
    """The live stream a Home's open clients follow (the stream hub).

    It is also the only thing that knows a turn is in progress in a
    conversation, which a thread's derived state depends on.
    """

    def has_running_turn(self, conversation_id: str) -> bool: ...

    async def publish(
        self,
        conversation_id: str,
        event_type: str,
        *,
        turn_id: str | None,
        payload: dict[str, object],
    ) -> object: ...

    async def publish_activity(
        self,
        conversation_id: str,
        *,
        user_id: str,
        reason: str,
        timestamp: datetime | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WrittenEcho:
    """An echo written into a Home."""

    home_conversation_id: str
    owner_user_id: str
    message_id: int


async def announce_home_change(
    publisher: HomeActivityPublisher | None,
    *,
    home_conversation_id: str,
    user_id: str,
    reason: str,
) -> None:
    """Tell a Home's open clients that it, or one of its threads' cards, changed.

    ``user_id`` is a raw login id: the activity feed matches subscribers by the
    exact id their login carries, as every other publisher addresses it.
    """
    if publisher is None:
        return
    await publisher.publish(
        home_conversation_id,
        "message",
        turn_id=None,
        payload={"conversation_id": home_conversation_id, "new_messages": True},
    )
    await publisher.publish_activity(
        home_conversation_id, user_id=user_id, reason=reason
    )


async def echo_finished_turn(
    db: DatabaseExecutor,
    *,
    conversation_id: str,
    turn_id: str,
    home_profile: ProcessingServiceConfig,
    now: datetime | None = None,
) -> WrittenEcho | None:
    """Echo the final message of ``turn_id`` if the turn closed its thread.

    Does nothing unless ``conversation_id`` is a thread its assistant closed in
    this turn and the close has not been echoed yet. A closing turn that ended
    without a visible message leaves nothing to echo, and the thread is closed
    without one.
    """
    thread = await db.conversation_threads.get_thread(conversation_id)
    close = thread.close if thread is not None else None
    if (
        thread is None
        or close is None
        or close.closed_by != "assistant"
        or close.turn_id != turn_id
        or close.echo_message_id is not None
    ):
        return None
    rows = await db.message_history.get_rows_by_turn_id(turn_id)
    replies = [
        row
        for row in rows
        if row["role"] == "assistant"
        and row["conversation_id"] == conversation_id
        and row["subconversation_id"] is None
    ]
    # Only the turn's terminal reply is the outcome: text written alongside a
    # tool call -- the finish_thread call itself included -- is a step, and a
    # turn that ended quietly or empty has no outcome to report.
    final = replies[-1] if replies else None
    if (
        final is None
        or final["tool_calls"]
        or final["is_internal"]
        or not isinstance(final["content"], str)
        or not final["content"].strip()
    ):
        logger.info(
            "Thread %s was closed in turn %s, which ended with no message to echo.",
            conversation_id,
            turn_id,
        )
        return None
    moment = now or datetime.now(UTC)

    async def _write(txn: DatabaseTransaction) -> int:
        [message_id] = await copy_across_boundary(
            txn,
            destination=BoundaryDestination(
                interface_type=thread.interface_type,
                conversation_id=thread.home_conversation_id,
                profile=home_profile,
                user_id=thread.owner_user_id,
            ),
            payload=ThreadEcho(
                thread_conversation_id=conversation_id,
                message_id=final["internal_id"],
            ),
            turn_id=str(uuid.uuid4()),
            now=moment,
        )
        recorded = await txn.conversation_threads.record_echo(
            conversation_id, turn_id=turn_id, echo_message_id=message_id
        )
        if not recorded:
            # Closed again, or echoed, between the read above and now: roll the
            # copy back rather than echo one close twice.
            raise _EchoSupersededError
        return message_id

    try:
        message_id = await db.atomic(_write)
    except _EchoSupersededError:
        return None
    logger.info(
        "Echoed thread %s's outcome into Home %s as message %s",
        conversation_id,
        thread.home_conversation_id,
        message_id,
    )
    return WrittenEcho(
        home_conversation_id=thread.home_conversation_id,
        owner_user_id=thread.owner_user_id,
        message_id=message_id,
    )


class _EchoSupersededError(Exception):
    """The close being echoed was replaced or echoed while the copy was written."""
