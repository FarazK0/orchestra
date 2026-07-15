"""Add stream_deliveries table for Redis Streams exactly-once dedup.

Revision ID: 002
Revises: 001
Create Date: 2026-07-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stream_deliveries",
        sa.Column("delivery_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("stream_key", sa.Text, nullable=False),
        sa.Column("message_id", sa.Text, nullable=False),
        sa.Column("consumer_group", sa.Text, nullable=False),
        sa.Column("event_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_stream_delivery",
        "stream_deliveries",
        ["stream_key", "message_id", "consumer_group"],
    )


def downgrade() -> None:
    op.drop_table("stream_deliveries")
