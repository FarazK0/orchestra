"""Redis Streams publisher and consumer for the Orchestra event bus.

Architecture
------------
- One stream per project: ``orchestra:events``
- One consumer group per agent type: ``backend-agent``, ``frontend-agent``, etc.
- Postgres ``stream_deliveries`` table provides the exactly-once dedup guarantee.

Delivery guarantee
------------------
XACK is sent AFTER the Postgres ``stream_deliveries`` row is committed.  If the
process dies between commit and XACK the message re-appears as a pending entry;
the dedup check on the next ``reclaim_pending`` call prevents double-processing.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import redis
import redis.exceptions
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import StreamDelivery


STREAM_KEY = "orchestra:events"


def get_redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6380")


class StreamPublisher:
    """Publishes events to the Redis Stream."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._r = redis.Redis.from_url(redis_url or get_redis_url(), decode_responses=True)

    def publish(
        self,
        event_id: str,
        event_type: str,
        task_id: str | None,
        payload: dict[str, Any],
    ) -> str:
        """XADD to the stream. Returns the Redis message_id (e.g. '1234567890123-0')."""
        fields = {
            "event_id": event_id,
            "event_type": event_type,
            "task_id": task_id or "",
            "payload": json.dumps(payload),
        }
        return self._r.xadd(STREAM_KEY, fields)

    def close(self) -> None:
        self._r.close()


class StreamConsumer:
    """Reads from the Redis Stream with consumer-group semantics and Postgres dedup."""

    RECLAIM_IDLE_MS: int = 30_000  # reclaim pending entries idle > 30 s

    def __init__(
        self,
        consumer_group: str,
        consumer_name: str,
        session_factory: Callable[[], Session],
        redis_url: str | None = None,
    ) -> None:
        self._r = redis.Redis.from_url(redis_url or get_redis_url(), decode_responses=True)
        self.stream_key = STREAM_KEY
        self.group = consumer_group
        self.name = consumer_name
        self._session_factory = session_factory

    def ensure_group(self) -> None:
        """Create the consumer group (and stream if absent). Idempotent."""
        try:
            self._r.xgroup_create(self.stream_key, self.group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def reclaim_pending(self, idle_ms: int | None = None) -> int:
        """Claim unacknowledged entries from dead consumers.

        Entries idle longer than ``idle_ms`` (default: RECLAIM_IDLE_MS) are
        transferred to this consumer so the next ``consume_one`` call picks them up.

        Returns the number of entries reclaimed.
        """
        threshold = idle_ms if idle_ms is not None else self.RECLAIM_IDLE_MS
        pending = self._r.xpending_range(self.stream_key, self.group, "-", "+", count=1000)
        claimed = 0
        for entry in pending:
            if entry["time_since_delivered"] >= threshold:
                self._r.xclaim(
                    self.stream_key,
                    self.group,
                    self.name,
                    threshold,
                    [entry["message_id"]],
                )
                claimed += 1
        return claimed

    def consume_one(
        self,
        handler: Callable[[dict[str, str]], None],
        block_ms: int = 1000,
    ) -> bool:
        """Read up to 10 messages and process each through the dedup pipeline.

        Returns True if at least one message was processed, False if the stream
        was empty for the full ``block_ms`` window.
        """
        results = self._r.xreadgroup(
            self.group,
            self.name,
            {self.stream_key: ">"},
            count=10,
            block=block_ms,
        )
        if not results:
            return False
        for _stream, messages in results:
            for message_id, fields in messages:
                self._process(message_id, fields, handler)
        return True

    def _process(
        self,
        message_id: str,
        fields: dict[str, str],
        handler: Callable[[dict[str, str]], None],
    ) -> None:
        """Dedup check → handler → Postgres commit → XACK."""
        with self._session_factory() as session:
            already_done = session.execute(
                select(StreamDelivery).where(
                    StreamDelivery.stream_key == self.stream_key,
                    StreamDelivery.message_id == message_id,
                    StreamDelivery.consumer_group == self.group,
                )
            ).scalar_one_or_none()

            if already_done is not None:
                # Duplicate delivery — ack without re-running the handler.
                self._r.xack(self.stream_key, self.group, message_id)
                return

            handler(fields)

            raw_event_id = fields.get("event_id", "")
            event_uuid: uuid.UUID | None = None
            if raw_event_id:
                try:
                    event_uuid = uuid.UUID(raw_event_id)
                except ValueError:
                    pass

            session.add(
                StreamDelivery(
                    stream_key=self.stream_key,
                    message_id=message_id,
                    consumer_group=self.group,
                    event_id=event_uuid,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        # XACK after commit — if we die here, the pending-entry reclaim + dedup
        # table prevents double-processing on the next restart.
        self._r.xack(self.stream_key, self.group, message_id)

    def close(self) -> None:
        self._r.close()
