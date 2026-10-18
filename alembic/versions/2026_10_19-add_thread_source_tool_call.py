"""Record the Home tool call that started a thread.

Revision ID: add_thread_source_tool_call
Revises: add_conversation_threads
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_thread_source_tool_call"
down_revision: str | None = "add_conversation_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_threads") as batch:
        batch.add_column(
            sa.Column("source_tool_call_id", sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_threads") as batch:
        batch.drop_column("source_tool_call_id")
