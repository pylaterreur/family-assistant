"""The one place rows cross from one conversation into another.

Every path that carries content between a Home and its threads goes through
:func:`copy_across_boundary`, so a path added later cannot skip a rule. It
applies three:

1. **Taint and profile.** Copied rows keep their taint exactly and are
   re-stamped to the destination's profile, because history windows are scoped
   by profile and an unstamped row would be invisible there.
2. **The context gate.** Home turns, their tool output included, may only enter
   a profile that already receives household context. Any other destination
   accepts request text only.
3. **Authorship.** Only text the user typed is stored as a user row. A request
   the assistant wrote is stored as machine-authored data -- an internal user
   row, as delegation results are -- so it never counts as user activity or as
   evidence of what the user said. A finished thread's echo is the thread
   assistant's own message, so it stays an assistant row.

See docs/design/one-thread-chat-ux.md, "Moving content across the boundary".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.llm.messages import UserMessage
from family_assistant.security.taint import (
    TurnTaintState,
    floor_machine_authored_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.processing.types import ProcessingServiceConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseExecutor


class CrossBoundaryCopyRefusedError(Exception):
    """The destination's profile may not receive the content offered to it."""


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    """A request the assistant wrote to open a thread.

    ``taint`` is the taint of the turn that wrote it, which the seed carries.
    """

    text: str
    taint: TaintMetadata | None
    attachments: Sequence[MessageAttachmentMetadata] = ()


@dataclass(frozen=True, slots=True)
class CopiedTurns:
    """Whole top-level turns of a source conversation, copied verbatim."""

    source_interface_type: str
    source_conversation_id: str
    turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadEcho:
    """A finished thread's final assistant message, echoed into its Home."""

    thread_conversation_id: str
    message_id: int


@dataclass(frozen=True, slots=True)
class BoundaryDestination:
    """Where content is going: a conversation and the profile that reads it."""

    interface_type: str
    conversation_id: str
    profile: ProcessingServiceConfig
    user_id: str


async def copy_across_boundary(
    db: DatabaseExecutor,
    *,
    destination: BoundaryDestination,
    payload: AssistantRequest | CopiedTurns | ThreadEcho,
    turn_id: str,
    now: datetime,
) -> list[int]:
    """Write ``payload`` into ``destination`` under the three rules.

    ``turn_id`` is the turn an assistant request or an echo is written under;
    copied turns each get a fresh turn of their own. Returns the new rows' ids
    in order.

    Raises:
        CrossBoundaryCopyRefusedError: ``payload`` carries conversation turns
            and the destination's profile does not receive household context.
    """
    match payload:
        case AssistantRequest():
            return [
                await _write_assistant_request(
                    db,
                    destination=destination,
                    request=payload,
                    turn_id=turn_id,
                    now=now,
                )
            ]
        case ThreadEcho():
            # Thread content entering Home, which receives household context
            # already: the context gate guards Home content leaving it.
            return [
                await db.message_history.copy_message_as_echo(
                    source_internal_id=payload.message_id,
                    destination_conversation_id=destination.conversation_id,
                    destination_profile_id=destination.profile.id,
                    echoed_from_conversation_id=payload.thread_conversation_id,
                    turn_id=turn_id,
                    timestamp=now,
                )
            ]
        case CopiedTurns():
            if not destination.profile.receives_household_context:
                raise CrossBoundaryCopyRefusedError(
                    f"Profile {destination.profile.id!r} does not receive household "
                    "context, so conversation turns cannot be copied into it."
                )
            return await db.message_history.copy_turns(
                source_interface_type=payload.source_interface_type,
                source_conversation_id=payload.source_conversation_id,
                turn_ids=payload.turn_ids,
                destination_conversation_id=destination.conversation_id,
                destination_profile_id=destination.profile.id,
            )


async def _write_assistant_request(
    db: DatabaseExecutor,
    *,
    destination: BoundaryDestination,
    request: AssistantRequest,
    turn_id: str,
    now: datetime,
) -> int:
    # A turn with no taint tracker has introduced nothing untrusted, which is
    # how a delegation request with no recorded taint is treated too.
    taint = floor_machine_authored_metadata(
        request.taint
        if request.taint is not None
        else TurnTaintState.empty().to_metadata()
    )
    return await db.message_history.add_message(
        UserMessage(
            content=request.text,
            taint_metadata=taint,
            authorship_taint_metadata=taint,
        ),
        interface_type=destination.interface_type,
        conversation_id=destination.conversation_id,
        timestamp=now,
        turn_id=turn_id,
        processing_profile_id=destination.profile.id,
        user_id=destination.user_id,
        attachments=list(request.attachments) or None,
        is_internal=True,
    )
