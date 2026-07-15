"""Initial control-plane schema: tasks, events, runs, audit.

Revision ID: 001
Revises:
Create Date: 2026-07-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("owner", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="created"),
        sa.Column("depends_on", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("inputs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("outputs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("acceptance", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("risk_tier", sa.Integer, nullable=False, server_default="1"),
        sa.Column("budget", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "events",
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("emitted_by", sa.String, nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    op.create_table(
        "runs",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("agent_id", sa.String, nullable=False),
        sa.Column("branch", sa.String, nullable=False),
        sa.Column("context_package_ref", sa.String, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=10, scale=6),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "audit",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("task_id", sa.String, nullable=True),
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("events.event_id"),
            nullable=False,
        ),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    # Index for fast lookup of events and audit rows by task
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_index("ix_audit_task_id", "audit", ["task_id"])
    op.create_index("ix_audit_event_id", "audit", ["event_id"])
    op.create_index("ix_runs_task_id", "runs", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_task_id", "runs")
    op.drop_index("ix_audit_event_id", "audit")
    op.drop_index("ix_audit_task_id", "audit")
    op.drop_index("ix_events_task_id", "events")
    op.drop_table("audit")
    op.drop_table("runs")
    op.drop_table("events")
    op.drop_table("tasks")
