"""Redis Streams contract tests.

All tests require Docker Compose to be running (make up):
  - Postgres on port 5433
  - Redis on port 6380

The redis_client fixture wipes the test stream after each test so tests
are isolated without requiring separate stream keys.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from orchestrator.orchestrator.db import StreamDelivery
from orchestrator.orchestrator.streams import STREAM_KEY, StreamConsumer, StreamPublisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(fields: dict) -> None:
    pass


def _publisher(redis_url: str) -> StreamPublisher:
    return StreamPublisher(redis_url)


def _consumer(
    redis_url: str,
    session_factory: Callable,
    group: str = "test-group",
    name: str = "consumer-a",
) -> StreamConsumer:
    return StreamConsumer(group, name, session_factory, redis_url)


# ---------------------------------------------------------------------------
# StreamPublisher
# ---------------------------------------------------------------------------


def test_publish_returns_message_id(redis_client, redis_url):
    pub = _publisher(redis_url)
    msg_id = pub.publish(str(uuid.uuid4()), "test_event", None, {"k": "v"})
    assert isinstance(msg_id, str)
    assert "-" in msg_id  # Redis IDs look like "1234567890123-0"
    pub.close()


# ---------------------------------------------------------------------------
# StreamConsumer — group management
# ---------------------------------------------------------------------------


def test_ensure_group_is_idempotent(redis_client, redis_url, session_factory):
    consumer = _consumer(redis_url, session_factory)
    consumer.ensure_group()
    consumer.ensure_group()  # must not raise BUSYGROUP
    consumer.close()


# ---------------------------------------------------------------------------
# StreamConsumer — dedup
# ---------------------------------------------------------------------------


def test_dedup_prevents_double_processing(redis_client, redis_url, session_factory, engine):
    """Calling _process twice with the same message_id invokes the handler once."""
    from sqlalchemy.orm import Session

    pub = _publisher(redis_url)
    consumer = _consumer(redis_url, session_factory)
    consumer.ensure_group()

    pub.publish(str(uuid.uuid4()), "dedup_test", None, {})

    call_count = 0

    def counting_handler(fields: dict) -> None:
        nonlocal call_count
        call_count += 1

    # Read the message manually so we have the real Redis message_id
    results = consumer._r.xreadgroup(
        consumer.group, consumer.name, {STREAM_KEY: ">"}, count=1, block=100
    )
    assert results, "expected one message"
    _, messages = results[0]
    redis_msg_id, fields = messages[0]

    # Process once
    consumer._process(redis_msg_id, fields, counting_handler)
    # Process again (simulate duplicate delivery)
    consumer._process(redis_msg_id, fields, counting_handler)

    assert call_count == 1

    with Session(engine) as s:
        rows = (
            s.query(StreamDelivery).filter_by(stream_key=STREAM_KEY, message_id=redis_msg_id).all()
        )
    assert len(rows) == 1

    pub.close()
    consumer.close()


# ---------------------------------------------------------------------------
# StreamConsumer — pending-entry reclaim
# ---------------------------------------------------------------------------


def test_reclaim_pending_returns_count(redis_client, redis_url, session_factory):
    """Entries delivered but not ACKed appear as pending; reclaim transfers them."""
    pub = _publisher(redis_url)
    consumer_a = _consumer(redis_url, session_factory, name="consumer-a")
    consumer_b = _consumer(redis_url, session_factory, name="consumer-b")
    consumer_a.ensure_group()

    pub.publish(str(uuid.uuid4()), "pending_test", None, {})

    # Deliver to consumer-a without ACKing (simulate crash)
    consumer_a._r.xreadgroup(
        consumer_a.group, consumer_a.name, {STREAM_KEY: ">"}, count=1, block=100
    )

    # Reclaim immediately (idle_ms=0)
    claimed = consumer_b.reclaim_pending(idle_ms=0)
    assert claimed == 1

    pub.close()
    consumer_a.close()
    consumer_b.close()


# ---------------------------------------------------------------------------
# Contract test: exactly-once across consumer restart
# ---------------------------------------------------------------------------


def test_exactly_once_across_restart(redis_client, redis_url, session_factory, engine):
    """Publish 100 events; consumer-a processes ~50 then 'crashes' (stops without
    ACKing the rest); consumer-b reclaims pending and finishes.
    Assert exactly 100 StreamDelivery rows in Postgres.
    """
    from sqlalchemy.orm import Session

    pub = _publisher(redis_url)
    group = "exactly-once-group"
    consumer_a = _consumer(redis_url, session_factory, group=group, name="consumer-a")
    consumer_b = _consumer(redis_url, session_factory, group=group, name="consumer-b")
    consumer_a.ensure_group()

    # Publish 100 events
    for i in range(100):
        pub.publish(str(uuid.uuid4()), "batch_event", None, {"seq": i})

    # Consumer A processes messages until it has committed 50, then stops.
    # consume_one processes up to 10 at a time, so we loop until >= 50 are done.
    while True:
        with Session(engine) as s:
            done = s.query(StreamDelivery).filter_by(consumer_group=group).count()
        if done >= 50:
            break
        consumer_a.consume_one(_noop, block_ms=100)

    # Simulate crash: consumer_a stops; remaining are pending entries.

    # Consumer B reclaims all pending entries (idle_ms=0 in tests).
    consumer_b.reclaim_pending(idle_ms=0)

    # Consumer B drains the stream.
    while consumer_b.consume_one(_noop, block_ms=100):
        pass

    with Session(engine) as s:
        total = s.query(StreamDelivery).filter_by(consumer_group=group).count()

    assert total == 100

    pub.close()
    consumer_a.close()
    consumer_b.close()
