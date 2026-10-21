"""Where each MCP event subscription has read up to."""

from sqlalchemy import Column, DateTime, String, Table, Text
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

# One row per subscription: a server, an event type, its arguments, and the
# user whose credential polls it. ``subscription_key`` is a digest of those,
# so listeners that watch the same thing as the same person share a position.
mcp_event_cursors_table = Table(
    "mcp_event_cursors",
    metadata,
    Column("subscription_key", String(64), primary_key=True),
    Column("server_id", String(255), nullable=False),
    Column("event_name", String(255), nullable=False),
    Column("cursor", Text, nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
