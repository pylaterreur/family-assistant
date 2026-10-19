"""Record explicit thread closes, echoes, and per-viewer read markers.

Revision ID: add_thread_close_read_markers
Revises: add_thread_source_tool_call
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_thread_close_read_markers"
down_revision: str | None = "add_thread_source_tool_call"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_threads") as batch:
        batch.add_column(
            sa.Column("closed_outcome", sa.String(length=20), nullable=True)
        )
        batch.add_column(sa.Column("closed_by", sa.String(length=20), nullable=True))
        batch.add_column(
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("closed_turn_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(sa.Column("echo_message_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("message_history") as batch:
        batch.add_column(
            sa.Column(
                "echoed_from_conversation_id", sa.String(length=255), nullable=True
            )
        )
    op.create_table(
        "conversation_read_markers",
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("viewer_user_id", sa.String(length=255), nullable=False),
        sa.Column("last_read_message_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id", "viewer_user_id"),
    )


def downgrade() -> None:
    op.drop_table("conversation_read_markers")
    with op.batch_alter_table("message_history") as batch:
        batch.drop_column("echoed_from_conversation_id")
    with op.batch_alter_table("conversation_threads") as batch:
        batch.drop_column("echo_message_id")
        batch.drop_column("closed_turn_id")
        batch.drop_column("closed_at")
        batch.drop_column("closed_by")
        batch.drop_column("closed_outcome")
