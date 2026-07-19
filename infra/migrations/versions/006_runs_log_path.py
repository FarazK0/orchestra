"""Add log_path column to runs table for per-run agent log files.

Revision ID: 006
Revises: 005
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("log_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "log_path")
