"""
student_service/database.py
Async SQLite database connection & session lifecycle for Student Service.
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

from student_service.models import Base

# Resolve persistent student.db location (default to project workspace root)
DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "student.db"
DATABASE_URL = os.getenv(
    "STUDENT_DATABASE_URL",
    f"sqlite+aiosqlite:///{DEFAULT_DB_FILE.as_posix()}",
)
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "15000"))


def make_engine(url: str) -> AsyncEngine:
    """Create an async SQLite engine configured with WAL and busy timeout."""
    eng = create_async_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    if "sqlite" in url:
        @event.listens_for(eng.sync_engine, "connect")
        def _on_connect(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

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
    """Initialize database tables for Student Profile Service."""
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
