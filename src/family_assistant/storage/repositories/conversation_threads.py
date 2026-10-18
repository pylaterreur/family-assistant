"""Repository for Home conversations and the threads spun off them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.storage.conversation_threads import (
    conversation_homes_table,
    conversation_threads_table,
)
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from datetime import datetime

    from family_assistant.security.taint import TaintMetadata


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """A conversation recorded as a thread of a user's Home."""

    conversation_id: str
    home_conversation_id: str
    interface_type: str
    owner_user_id: str
    profile_id: str
    title: str
    title_taint: TaintMetadata | None
    anchor_message_id: int | None
    seed_message_id: int | None
    source_tool_call_id: str | None
    delegation_id: str | None
    created_at: datetime
    updated_at: datetime


def _thread_from_row(row: Mapping[str, Any]) -> ConversationThread:
    return ConversationThread(
        conversation_id=row["conversation_id"],
        home_conversation_id=row["home_conversation_id"],
        interface_type=row["interface_type"],
        owner_user_id=row["owner_user_id"],
        profile_id=row["profile_id"],
        title=row["title"],
        title_taint=row["title_taint_json"],
        anchor_message_id=row["anchor_message_id"],
        seed_message_id=row["seed_message_id"],
        source_tool_call_id=row["source_tool_call_id"],
        delegation_id=row["delegation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ConversationThreadsRepository(BaseRepository):
    """Designate Homes and record the threads that hang off them."""

    async def get_home(self, user_id: str, interface_type: str) -> str | None:
        """Return the user's Home conversation on this interface, if designated."""
        row = await self._db.fetch_one(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.user_id == user_id,
                conversation_homes_table.c.interface_type == interface_type,
            )
        )
        return row["conversation_id"] if row is not None else None

    async def find_home_among(
        self, user_ids: Collection[str], interface_type: str
    ) -> str | None:
        """A Home stored under any of ``user_ids`` on this interface, if one exists."""
        if not user_ids:
            return None
        rows = await self._db.fetch_all(
            select(conversation_homes_table.c.conversation_id)
            .where(
                conversation_homes_table.c.user_id.in_(list(user_ids)),
                conversation_homes_table.c.interface_type == interface_type,
            )
            .order_by(conversation_homes_table.c.created_at)
            .limit(1)
        )
        return rows[0]["conversation_id"] if rows else None

    async def designate_home(
        self,
        *,
        user_id: str,
        interface_type: str,
        conversation_id: str,
        now: datetime,
    ) -> str:
        """Make ``conversation_id`` the user's Home unless one already exists.

        Returns the Home that is in effect afterwards, which is the existing
        one when two callers race.
        """
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        await self._db.execute(
            insert_ctor(conversation_homes_table)
            .values(
                user_id=user_id,
                interface_type=interface_type,
                conversation_id=conversation_id,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        home = await self.get_home(user_id, interface_type)
        if home is None:
            raise RuntimeError(
                f"No Home for {user_id!r} on {interface_type!r} after designating "
                f"{conversation_id!r}; the conversation is already another user's Home"
            )
        return home

    async def is_home(self, conversation_id: str) -> bool:
        """Whether a conversation is some user's Home."""
        row = await self._db.fetch_one(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.conversation_id == conversation_id
            )
        )
        return row is not None

    async def kinds_for(
        self, conversation_ids: Collection[str]
    ) -> dict[str, Literal["home", "thread"]]:
        """Which of ``conversation_ids`` are Homes or threads; others are absent."""
        if not conversation_ids:
            return {}
        ids = list(conversation_ids)
        homes = await self._db.fetch_all(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.conversation_id.in_(ids)
            )
        )
        threads = await self._db.fetch_all(
            select(conversation_threads_table.c.conversation_id).where(
                conversation_threads_table.c.conversation_id.in_(ids)
            )
        )
        kinds: dict[str, Literal["home", "thread"]] = {
            row["conversation_id"]: "thread" for row in threads
        }
        kinds.update({row["conversation_id"]: "home" for row in homes})
        return kinds

    async def create_thread(
        self,
        *,
        conversation_id: str,
        home_conversation_id: str,
        interface_type: str,
        owner_user_id: str,
        profile_id: str,
        title: str,
        title_taint: TaintMetadata | None,
        anchor_message_id: int | None,
        delegation_id: str | None,
        now: datetime,
        source_tool_call_id: str | None = None,
    ) -> None:
        """Record a new thread of ``home_conversation_id``."""
        await self._db.execute(
            conversation_threads_table.insert().values(
                conversation_id=conversation_id,
                home_conversation_id=home_conversation_id,
                interface_type=interface_type,
                owner_user_id=owner_user_id,
                profile_id=profile_id,
                title=title,
                title_taint_json=title_taint,
                anchor_message_id=anchor_message_id,
                seed_message_id=None,
                source_tool_call_id=source_tool_call_id,
                delegation_id=delegation_id,
                created_at=now,
                updated_at=now,
            )
        )

    async def set_seed_message(
        self, conversation_id: str, seed_message_id: int, now: datetime
    ) -> None:
        """Record the row that opened the thread."""
        await self._db.execute(
            conversation_threads_table
            .update()
            .where(conversation_threads_table.c.conversation_id == conversation_id)
            .values(seed_message_id=seed_message_id, updated_at=now)
        )

    async def get_thread(self, conversation_id: str) -> ConversationThread | None:
        """Return the thread record for a conversation, or None if it is not one."""
        row = await self._db.fetch_one(
            select(conversation_threads_table).where(
                conversation_threads_table.c.conversation_id == conversation_id
            )
        )
        return _thread_from_row(row) if row is not None else None

    async def list_for_home(
        self, home_conversation_id: str
    ) -> list[ConversationThread]:
        """Threads of a Home, newest first."""
        rows = await self._db.fetch_all(
            select(conversation_threads_table)
            .where(
                conversation_threads_table.c.home_conversation_id
                == home_conversation_id
            )
            .order_by(conversation_threads_table.c.created_at.desc())
        )
        return [_thread_from_row(row) for row in rows]
