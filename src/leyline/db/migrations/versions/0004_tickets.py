"""Tickets and their event trail.

Revision ID: 0004_tickets
Revises: 0003_conversations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_tickets"
down_revision = "0003_conversations"
branch_labels = None
depends_on = None

_NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requester_id", sa.Text(), nullable=False),
        sa.Column("requester_name", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("assignee_id", sa.Text(), nullable=True),
        sa.Column("assignee_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="ck_tickets_status",
        ),
    )
    op.create_index(
        "ix_tickets_requester_updated", "tickets", ["requester_id", "updated_at"]
    )
    op.create_index("ix_tickets_status_updated", "tickets", ["status", "updated_at"])
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.Uuid(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('created', 'comment', 'status_changed', 'assigned')",
            name="ck_ticket_events_kind",
        ),
    )
    op.create_index("ix_ticket_events_ticket", "ticket_events", ["ticket_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_events_ticket", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_status_updated", table_name="tickets")
    op.drop_index("ix_tickets_requester_updated", table_name="tickets")
    op.drop_table("tickets")
