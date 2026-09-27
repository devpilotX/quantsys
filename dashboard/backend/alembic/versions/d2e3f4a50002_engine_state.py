"""engine_state: the decision engine's state, one row per mode

Revision ID: d2e3f4a50002
Revises: c1d2e3f40001
Create Date: 2026-09-26 00:00:00

The live and paper runners restore this row after a restart. Without it the
daily 08:50 recycle rebuilt the engine from nothing: kill latches cleared,
the drawdown reference re-based to the morning's equity, and every sleeve's
hold counter and rebalance clock reset.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2e3f4a50002"
down_revision: str | None = "c1d2e3f40001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engine_state",
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
        sa.Column("bar_ts", sa.DateTime(), nullable=True),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON().with_variant(
            postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("basis", sa.JSON().with_variant(
            postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.PrimaryKeyConstraint("mode"),
    )


def downgrade() -> None:
    op.drop_table("engine_state")
