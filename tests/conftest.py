"""Shared real-Postgres fixtures for Pathfinder test suites (tests/scoring,
tests/booklog). Runs against a dedicated `polymarket_test` database (same
Postgres server as the tracker, separate database) — never against
production `polymarket`, per the live-system ground rules. Schema is created
by running `alembic upgrade head` with DATABASE_URL pointed at
polymarket_test before running these suites; this conftest only asserts the
schema is present, it does not run migrations itself.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL = os.environ.get(
    "PATHFINDER_TEST_DATABASE_URL",
    "postgresql+psycopg://poly:poly@localhost:5432/polymarket_test",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Truncated after every test. Add new Pathfinder-owned or test-seeded tables
# here as new suites need them.
_TRUNCATE_TABLES = (
    "wallet_closed_positions",
    "wallet_positions",
    "wallets",
    "wallet_scores_pit",
    "book_snapshots",
    "tokens",
    "markets",
    "event_clusters",
    "trades",
    "signals",
)


@pytest.fixture(scope="session")
def event_loop_policy():
    # psycopg async cannot use Windows' default ProactorEventLoop — see run.py.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def _expected_head_revision() -> str:
    """Read the migrations directory's own head — not a hardcoded constant,
    so this check never goes stale as new migrations are added."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@pytest.fixture(scope="session")
def _verify_test_db_migrated():
    """Fail loudly (not silently migrate) if polymarket_test isn't at head.

    NOT autouse — only fixtures that actually touch polymarket_test (db_session,
    session_factory) depend on this, so mock-only unit tests never pay for or
    get blocked by Pathfinder's test-DB migration state.
    """
    import psycopg

    expected = _expected_head_revision()
    sync_url = TEST_DATABASE_URL.replace("postgresql+psycopg://", "")
    with psycopg.connect(f"postgresql://{sync_url}") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
    assert row is not None and row[0] == expected, (
        f"polymarket_test is not at migration head ({expected}); "
        f"got {row}. Run: DATABASE_URL=postgresql+psycopg://poly:poly@"
        f"localhost:5432/polymarket_test python -m alembic upgrade head"
    )


@pytest.fixture
async def db_session(_verify_test_db_migrated):
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(_TRUNCATE_TABLES)} RESTART IDENTITY CASCADE")
        )
    await engine.dispose()


@pytest.fixture
async def session_factory(_verify_test_db_migrated):
    """A second, independent engine/session_factory against polymarket_test —
    for code under test that opens its own sessions (e.g. take_book_snapshot)
    rather than being handed one directly. Data committed via `db_session` in
    the same test is visible here (same physical test database)."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()
