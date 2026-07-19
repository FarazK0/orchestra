"""Test fixtures for the orchestrator package.

Tests run against a real Postgres instance (orchestra_test database).
Each test is wrapped in a transaction that is rolled back on teardown,
so tests are isolated without touching permanent state.

Requires the Docker Compose stack to be running: `make up`
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import redis as redis_lib
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

_BASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra",
)

# Derive the test DB URL: same host/user/pass, database name = <name>_test
_parsed = make_url(_BASE_URL)
TEST_DB_URL = _parsed.set(database=(_parsed.database or "orchestra") + "_test").render_as_string(
    hide_password=False
)


def _ensure_test_db(test_url: str) -> None:
    """Create the test database if it does not already exist.

    Uses a SQLAlchemy engine (not raw psycopg) so the same auth path that
    works for migrations is used here.  CREATE DATABASE requires autocommit.
    """
    from sqlalchemy import text

    db_name = make_url(test_url).database
    # Connect to the main orchestra DB; isolation_level=AUTOCOMMIT lets us
    # run CREATE DATABASE outside a transaction.
    admin_engine = create_engine(_BASE_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()


@pytest.fixture(scope="session")
def engine():
    """Session-scoped engine pointing at the test database.

    Creates the test DB if needed and builds the schema from ORM metadata.
    Drops the schema at the end of the test session.
    """
    from orchestrator.orchestrator.db import Base

    _ensure_test_db(TEST_DB_URL)
    eng = create_engine(TEST_DB_URL)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine):
    """Per-test Session whose transaction is always rolled back on teardown."""
    sess = Session(engine)
    sess.begin()
    yield sess
    sess.rollback()
    sess.close()


_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380")


@pytest.fixture(scope="session")
def redis_url() -> str:
    return _REDIS_URL


@pytest.fixture
def redis_client(redis_url: str):
    """Per-test Redis client. Deletes the test stream on teardown."""
    from orchestrator.orchestrator.streams import STREAM_KEY

    r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
    yield r
    r.delete(STREAM_KEY)
    r.close()


@pytest.fixture
def session_factory(engine):
    """Session factory (not rolled-back) for use by StreamConsumer internals."""
    factory = sessionmaker(engine)
    return factory


_DEFAULT_BUDGET = {"tokens": 100_000, "wall_clock_min": 30, "retries": 2}


@pytest.fixture(autouse=True)
def _reset_policy_singleton():
    """Reset the policy singleton before each test to avoid cross-test contamination."""
    from orchestrator.orchestrator.policy import reload_policy

    reload_policy()
    yield


def make_task(
    session: Session,
    task_id: str,
    status: str = "created",
    owner: str = "test-agent",
    title: str = "Test task",
    budget: dict | None = None,
) -> "Task":  # noqa: F821
    """Insert a Task row with the given status and return the ORM object."""
    from orchestrator.orchestrator.db import Task

    now = datetime.now(timezone.utc)
    task = Task(
        id=task_id,
        schema_version=1,
        title=title,
        owner=owner,
        status=status,
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=budget or _DEFAULT_BUDGET,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    return task
