"""Storage tables for Home conversations and the threads spun off them.

See docs/design/one-thread-chat-ux.md. A thread is an ordinary conversation;
these tables only record which conversation is a user's Home and which
conversations are threads of it.
"""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from family_assistant.storage.base import metadata

conversation_homes_table = Table(
    "conversation_homes",
    metadata,
    # The value written on the user's own message rows on this interface, so
    # ownership checks and Home lookups agree on who the user is.
    Column("user_id", String(255), nullable=False),
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("user_id", "interface_type"),
)

conversation_threads_table = Table(
    "conversation_threads",
    metadata,
    Column("conversation_id", String(255), primary_key=True),
    Column("home_conversation_id", String(255), nullable=False, index=True),
    Column("interface_type", String(50), nullable=False),
    # Canonical, like conversation_homes.user_id.
    Column("owner_user_id", String(255), nullable=False),
    Column("profile_id", String(100), nullable=False),
    Column("title", Text, nullable=False),
    # The taint of the turn that wrote the title. Home's assistant reads the
    # title as a thread fact, so it must carry where it came from.
    Column("title_taint_json", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    # The Home message the thread grew from, for the client's "Started from"
    # quote. Display only: never copied into the thread's history.
    Column("anchor_message_id", Integer, nullable=True),
    # The row that opened the thread, so clients can render an
    # assistant-written request as "Assistant asked: ...".
    Column("seed_message_id", Integer, nullable=True),
    # The Home tool call that started the thread, so a client rendering that
    # call can find the thread it made. Null for a thread the user started.
    Column("source_tool_call_id", String(255), nullable=True),
    # The run that executes the thread's first turn.
    Column("delegation_id", String(100), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
