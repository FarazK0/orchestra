"""Add adaptive lifecycle columns to tasks table.

Revision ID: 008
Revises: 007
Create Date: 2026-07-19

Adds four columns supporting v0.3 adaptive orchestration:
  parent_task_id  - FK to tasks(id); null for planner-originated tasks
  spawn_depth     - recursion depth of discovered tasks (root = 0)
  blocked_by      - JSONB list of child task IDs currently blocking this task
  checkpoint      - JSONB agent state snapshot stored at suspension time
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "parent_task_id",
            sa.String(),
            sa.ForeignKey("tasks.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("spawn_depth", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tasks",
        sa.Column("blocked_by", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "tasks",
        sa.Column("checkpoint", JSONB(), nullable=True),
    )
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_column("tasks", "checkpoint")
    op.drop_column("tasks", "blocked_by")
    op.drop_column("tasks", "spawn_depth")
    op.drop_column("tasks", "parent_task_id")
