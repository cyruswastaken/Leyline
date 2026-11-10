"""Durable, append-only security events; make admin audit events append-only too.

Revision ID: 0005_security_events
Revises: 0004_tickets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_security_events"
down_revision = "0004_tickets"
branch_labels = None
depends_on = None

_GUARDED = ("security_events", "audit_events")


def upgrade() -> None:
    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "details", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.create_index("ix_security_events_ts", "security_events", ["ts"])
    op.create_index(
        "ix_security_events_type_ts", "security_events", ["event_type", "ts"]
    )
    op.execute(
        """
        CREATE FUNCTION reject_event_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _GUARDED:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_event_change()"
        )


def downgrade() -> None:
    for table in _GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_event_change()")
    op.drop_index("ix_security_events_type_ts", table_name="security_events")
    op.drop_index("ix_security_events_ts", table_name="security_events")
    op.drop_table("security_events")
