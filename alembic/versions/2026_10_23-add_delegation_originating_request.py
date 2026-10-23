"""Carry a delegation run's originating request on the run

Revision ID: delegation_originating_request
Revises: add_turn_feedback
Create Date: 2026-10-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "delegation_originating_request"
down_revision: str | None = "add_turn_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("delegation_runs") as batch_op:
        batch_op.add_column(sa.Column("originating_request", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "originating_request_taint_json",
                sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("delegation_runs") as batch_op:
        batch_op.drop_column("originating_request_taint_json")
        batch_op.drop_column("originating_request")
