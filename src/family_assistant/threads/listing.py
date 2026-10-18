"""What a Home shows about its threads.

State is derived from facts rather than reported by the model (see
docs/design/one-thread-chat-ux.md, "State"). This milestone derives working,
waiting and failed; explicit done comes with the echo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.storage.repositories.conversation_threads import (
        ConversationThread,
    )

ThreadState = Literal["working", "waiting", "failed"]

_ACTIVE_RUN_STATUSES = frozenset({
    "queued",
    "running",
    "awaiting_children",
    "awaiting_remote",
})


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def derive_state(
    *,
    run_status: str | None,
    run_completed_at: datetime | None,
    work_in_progress: bool,
    latest_visible_at: datetime | None,
) -> ThreadState:
    """A thread's state from what is known about it.

    Working while anything is in progress in it: its starting run, a turn, or
    background work a turn handed off. Failed when the starting run failed and
    nothing has been said in the thread since. Anything else leaves the next
    move with the user.
    """
    if work_in_progress or run_status in _ACTIVE_RUN_STATUSES:
        return "working"
    if run_status == "failed" and (
        latest_visible_at is None
        or run_completed_at is None
        or _as_utc(latest_visible_at) <= _as_utc(run_completed_at)
    ):
        return "failed"
    return "waiting"


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """One thread as Home's card and the sidebar show it."""

    conversation_id: str
    title: str
    profile_id: str
    state: ThreadState
    source_tool_call_id: str | None
    seed_request: str | None
    preview: str | None
    created_at: datetime
    updated_at: datetime


async def list_home_threads(
    db: DatabaseExecutor,
    home_conversation_id: str,
    *,
    turn_running: Callable[[str], bool],
) -> list[ThreadSummary]:
    """The threads of a Home, newest first.

    ``turn_running`` says whether a conversation has a turn in progress, which
    only the live stream hub knows.
    """
    threads = await db.conversation_threads.list_for_home(home_conversation_id)
    return await summarize_threads(db, threads, turn_running=turn_running)


async def summarize_threads(
    db: DatabaseExecutor,
    threads: Sequence[ConversationThread],
    *,
    turn_running: Callable[[str], bool],
) -> list[ThreadSummary]:
    """What a card shows about each of ``threads``, in the same order."""
    if not threads:
        return []
    runs = await db.delegation_runs.get_by_delegation_ids([
        thread.delegation_id for thread in threads if thread.delegation_id
    ])
    latest = await db.message_history.latest_visible_messages([
        thread.conversation_id for thread in threads
    ])
    active = await db.delegation_runs.conversations_with_active_runs([
        thread.conversation_id for thread in threads
    ])
    seeds = await db.message_history.get_contents_by_internal_ids([
        thread.seed_message_id for thread in threads if thread.seed_message_id
    ])
    summaries: list[ThreadSummary] = []
    for thread in threads:
        run = runs.get(thread.delegation_id) if thread.delegation_id else None
        message = latest.get(thread.conversation_id)
        summaries.append(
            ThreadSummary(
                conversation_id=thread.conversation_id,
                title=thread.title,
                profile_id=thread.profile_id,
                state=derive_state(
                    run_status=run["status"] if run else None,
                    run_completed_at=run["completed_at"] if run else None,
                    work_in_progress=thread.conversation_id in active
                    or turn_running(thread.conversation_id),
                    latest_visible_at=message.timestamp if message else None,
                ),
                source_tool_call_id=thread.source_tool_call_id,
                seed_request=(
                    seeds.get(thread.seed_message_id)
                    if thread.seed_message_id
                    else None
                ),
                preview=message.content if message else None,
                created_at=_as_utc(thread.created_at),
                updated_at=max(
                    _as_utc(thread.updated_at),
                    _as_utc(message.timestamp if message else thread.updated_at),
                ),
            )
        )
    return summaries
