"""Add mcp_event_cursors for MCP event subscriptions.

Revision ID: add_mcp_event_cursors
Revises: add_moved_turn_marker
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_mcp_event_cursors"
down_revision: str | None = "add_moved_turn_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_event_cursors",
        sa.Column("subscription_key", sa.String(length=64), primary_key=True),
        sa.Column("server_id", sa.String(length=255), nullable=False),
        sa.Column("event_name", sa.String(length=255), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
    )


def downgrade() -> None:
    op.drop_table("mcp_event_cursors")
