"""Add turn_feedback for per-turn thumbs up/down ratings.

Revision ID: add_turn_feedback
Revises: add_mcp_event_cursors
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_turn_feedback"
down_revision: str | None = "add_mcp_event_cursors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turn_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.UniqueConstraint("turn_id", "user_id", name="uq_turn_feedback_turn_user"),
    )
    op.create_index("ix_turn_feedback_turn_id", "turn_feedback", ["turn_id"])


def downgrade() -> None:
    op.drop_index("ix_turn_feedback_turn_id", table_name="turn_feedback")
    op.drop_table("turn_feedback")
