"""Repository for per-turn thumbs up/down feedback."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import functions as func

from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.turn_feedback import turn_feedback_table

type TurnRating = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class TurnFeedback:
    """One person's rating of one turn."""

    turn_id: str
    user_id: str
    rating: TurnRating
    comment: str | None
    created_at: datetime
    updated_at: datetime


def _feedback_from_row(row: dict[str, object]) -> TurnFeedback:
    return TurnFeedback(
        turn_id=str(row["turn_id"]),
        user_id=str(row["user_id"]),
        rating=cast("TurnRating", row["rating"]),
        comment=cast("str | None", row["comment"]),
        created_at=cast("datetime", row["created_at"]),
        updated_at=cast("datetime", row["updated_at"]),
    )


class TurnFeedbackRepository(BaseRepository):
    """Store at most one rating per person per turn; rating again replaces it."""

    async def set(
        self,
        *,
        turn_id: str,
        user_id: str,
        rating: TurnRating,
        comment: str | None,
    ) -> TurnFeedback:
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        base_stmt = insert_ctor(turn_feedback_table).values(
            turn_id=turn_id,
            user_id=user_id,
            rating=rating,
            comment=comment,
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[
                turn_feedback_table.c.turn_id,
                turn_feedback_table.c.user_id,
            ],
            set_={
                "rating": base_stmt.excluded.rating,
                "comment": base_stmt.excluded.comment,
                "updated_at": func.now(),
            },
        )
        await self._db.execute(stmt)
        feedback = await self.get(turn_id=turn_id, user_id=user_id)
        if feedback is None:  # pragma: no cover - row exists after successful upsert
            raise RuntimeError("Turn feedback missing immediately after upsert")
        return feedback

    async def get(self, *, turn_id: str, user_id: str) -> TurnFeedback | None:
        row = await self._db.fetch_one(
            select(turn_feedback_table).where(
                turn_feedback_table.c.turn_id == turn_id,
                turn_feedback_table.c.user_id == user_id,
            )
        )
        return _feedback_from_row(row) if row is not None else None

    async def for_turns(
        self, *, turn_ids: Collection[str], user_id: str
    ) -> dict[str, TurnFeedback]:
        """The caller's ratings of *turn_ids*, keyed by turn id."""
        if not turn_ids:
            return {}
        rows = await self._db.fetch_all(
            select(turn_feedback_table).where(
                turn_feedback_table.c.turn_id.in_(list(turn_ids)),
                turn_feedback_table.c.user_id == user_id,
            )
        )
        return {str(row["turn_id"]): _feedback_from_row(row) for row in rows}

    async def clear(self, *, turn_id: str, user_id: str) -> bool:
        result = await self._db.execute(
            delete(turn_feedback_table).where(
                turn_feedback_table.c.turn_id == turn_id,
                turn_feedback_table.c.user_id == user_id,
            )
        )
        return result.rowcount > 0
