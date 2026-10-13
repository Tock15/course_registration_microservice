import asyncio
import random
from collections.abc import Callable, Coroutine
from sqlite3 import OperationalError as SqliteOperationalError
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from enrollment_service.config import DATABASE_URL, SQLITE_BUSY_TIMEOUT_MS
from enrollment_service.models import Base


def make_engine(url: str) -> AsyncEngine:
    """Create an async SQLite engine with WAL, busy timeout and BEGIN IMMEDIATE."""
    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


T = TypeVar("T")


async def run_with_retry(
    fn: Callable[[], Coroutine[Any, Any, T]],
    max_retries: int = 5,
    initial_delay: float = 0.025,
) -> T:
    """Execute async database operation with exponential backoff on SQLite lock contention."""
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            return await fn()
        except DBAPIError as exc:
            orig = getattr(exc, "orig", None)
            if (
                orig is not None
                and isinstance(orig, SqliteOperationalError)
                and "locked" in str(orig).lower()
            ):
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(delay + random.uniform(0.005, 0.02))
                delay *= 2
            else:
                raise


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; each use case opens one short session per transaction."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


engine = make_engine(DATABASE_URL)
async_session_maker = make_session_maker(engine)


async def init_db(bind: AsyncEngine = engine) -> None:
    """Initialize database tables for the Enrollment Service."""
    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency yielding the session factory (overridden in tests)."""
    return async_session_maker
