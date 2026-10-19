"""Mark the Home row that records a turn moved into a thread.

Revision ID: add_moved_turn_marker
Revises: add_thread_close_read_markers
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_moved_turn_marker"
down_revision: str | None = "add_thread_close_read_markers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.add_column(
            sa.Column("moved_to_conversation_id", sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.drop_column("moved_to_conversation_id")
