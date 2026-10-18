"""Add Home conversations, conversation threads, and thread delegation runs.

A thread's first turn runs as a delegation run whose ``conversation_id`` is
the thread itself, with no subconversation, so ``subconversation_id`` becomes
nullable and ``run_kind`` tells the two kinds apart.

Revision ID: add_conversation_threads
Revises: delete_dup_web_callback_replies
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_conversation_threads"
down_revision: str | None = "delete_dup_web_callback_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_homes",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("interface_type", sa.String(length=50), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "interface_type"),
        sa.UniqueConstraint("conversation_id"),
    )
    op.create_table(
        "conversation_threads",
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("home_conversation_id", sa.String(length=255), nullable=False),
        sa.Column("interface_type", sa.String(length=50), nullable=False),
        sa.Column("owner_user_id", sa.String(length=255), nullable=False),
        sa.Column("profile_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "title_taint_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("anchor_message_id", sa.Integer(), nullable=True),
        sa.Column("seed_message_id", sa.Integer(), nullable=True),
        sa.Column("delegation_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "ix_conversation_threads_home_conversation_id",
        "conversation_threads",
        ["home_conversation_id"],
    )
    with op.batch_alter_table("delegation_runs") as batch:
        batch.alter_column(
            "subconversation_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "run_kind",
                sa.String(length=20),
                nullable=False,
                server_default="delegation",
            )
        )
        batch.add_column(sa.Column("turn_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM delegation_runs WHERE run_kind = 'thread'"))
    with op.batch_alter_table("delegation_runs") as batch:
        batch.drop_column("turn_id")
        batch.drop_column("run_kind")
        batch.alter_column(
            "subconversation_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
    op.drop_index(
        "ix_conversation_threads_home_conversation_id",
        table_name="conversation_threads",
    )
    op.drop_table("conversation_threads")
    op.drop_table("conversation_homes")
