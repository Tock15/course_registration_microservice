"""
catalog_service/database.py
Async SQLite database connection & session lifecycle using SQLAlchemy 2.0.
"""
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from catalog_service.models import Base

# Resolve persistent catalog.db location (default to project workspace root)
DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "catalog.db"
DATABASE_URL = os.getenv(
    "CATALOG_DATABASE_URL",
    f"sqlite+aiosqlite:///{DEFAULT_DB_FILE.as_posix()}",
)
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))


def make_engine(url: str) -> AsyncEngine:
    """Create an async SQLite engine configured with WAL, busy timeout and BEGIN IMMEDIATE (ADR-001)."""
    eng = create_async_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    if "sqlite" in url:
        @event.listens_for(eng.sync_engine, "connect")
        def _on_connect(dbapi_connection, _connection_record) -> None:
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

        @event.listens_for(eng.sync_engine, "begin")
        def _on_begin(conn) -> None:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return eng


# Create asynchronous engine
engine = make_engine(DATABASE_URL)

# Async session factory
async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db(bind: AsyncEngine = engine) -> None:
    """Initialize database tables for Course Catalog Service."""
    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
