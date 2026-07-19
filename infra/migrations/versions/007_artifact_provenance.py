"""Add artifact_provenance table for per-file provenance tracking.

Revision ID: 007
Revises: 006
Create Date: 2026-07-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifact_provenance",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False, server_default="agent"),
        sa.Column("set_by_task", sa.String(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_path", "file_path", name="uq_artifact_provenance"),
    )


def downgrade() -> None:
    op.drop_table("artifact_provenance")
