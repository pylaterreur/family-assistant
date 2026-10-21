"""Repository for MCP event subscription cursors."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import functions as func

from family_assistant.storage.mcp_event_cursors import mcp_event_cursors_table

from .base import BaseRepository


class MCPEventCursorsRepository(BaseRepository):
    """Persist how far each MCP event subscription has been delivered."""

    async def get(self, subscription_key: str) -> str | None:
        """The stored cursor, or None if this subscription has never polled."""
        row = await self._db.fetch_one(
            select(mcp_event_cursors_table.c.cursor).where(
                mcp_event_cursors_table.c.subscription_key == subscription_key
            )
        )
        return row["cursor"] if row else None

    async def set(
        self,
        *,
        subscription_key: str,
        server_id: str,
        event_name: str,
        cursor: str,
    ) -> None:
        """Record the position the next poll resumes from."""
        insert = (
            postgresql_insert
            if self._db.dialect_name == "postgresql"
            else sqlite_insert
        )
        stmt = insert(mcp_event_cursors_table).values(
            subscription_key=subscription_key,
            server_id=server_id,
            event_name=event_name,
            cursor=cursor,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[mcp_event_cursors_table.c.subscription_key],
            set_={"cursor": cursor, "updated_at": func.now()},
        )
        await self._db.execute(stmt)
