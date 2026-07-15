"""SQLAlchemy ORM models for the Orchestra control plane.

Five tables:
  tasks            - mutable task state (status is the only frequently-written column)
  events           - append-only event log (never UPDATE or DELETE)
  runs             - one row per agent run attempt
  audit            - one row per auditable action, joined to the triggering event
  stream_deliveries - dedup table for Redis Streams exactly-once processing
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="created")
    depends_on: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    inputs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    outputs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    acceptance: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    risk_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    budget: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Event(Base):
    """Append-only event log. Rows are never updated or deleted."""

    __tablename__ = "events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, ForeignKey("tasks.id"), nullable=True)
    emitted_by: Mapped[str] = mapped_column(String, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str] = mapped_column(String, nullable=False)
    context_package_ref: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(precision=10, scale=6), nullable=False, default=0
    )


class AuditRow(Base):
    __tablename__ = "audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id"), nullable=False
    )
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class StreamDelivery(Base):
    """Dedup table for Redis Streams — ensures exactly-once processing per consumer group."""

    __tablename__ = "stream_deliveries"

    delivery_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    stream_key: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # Redis msg ID e.g. "1234567890-0"
    consumer_group: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("stream_key", "message_id", "consumer_group", name="uq_stream_delivery"),
    )


def get_engine(url: str | None = None):
    url = url or os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra",
    )
    return create_engine(url)


def get_session_factory(engine=None):
    if engine is None:
        engine = get_engine()
    return sessionmaker(engine)
