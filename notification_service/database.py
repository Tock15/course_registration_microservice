"""
notification_service/database.py
Async database engine, session factory, and SQLite concurrency hardening.
"""
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from notification_service.config import DATABASE_URL, SQLITE_BUSY_TIMEOUT_MS
from notification_service.models import Base


def make_engine(
    url: str = DATABASE_URL, busy_timeout: int = SQLITE_BUSY_TIMEOUT_MS
) -> AsyncEngine:
    """Create async engine with SQLite WAL mode and busy timeout pragmas."""
    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )

    if "sqlite" in url:
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
            cursor.close()

    return engine


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


engine = make_engine(DATABASE_URL)
async_session_maker = make_session_maker(engine)


async def init_db(bind: AsyncEngine = engine) -> None:
    """Initialize database tables for the Notification Service."""
    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a short-lived AsyncSession per request."""
    async with async_session_maker() as session:
        yield session
