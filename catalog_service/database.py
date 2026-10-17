"""
catalog_service/database.py
Async SQLite database connection & session lifecycle using SQLAlchemy 2.0.
"""
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from catalog_service.models import Base

logger = logging.getLogger(__name__)

# Resolve persistent catalog.db location (default to project workspace root)
DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "catalog.db"
DATABASE_URL = os.getenv(
    "CATALOG_DATABASE_URL",
    os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DEFAULT_DB_FILE.as_posix()}"),
)
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "15000"))


def make_engine(
    url: str,
    pool_size: int = 20,
    max_overflow: int = 20,
) -> AsyncEngine:
    """Create an async engine supporting SQLite (with WAL) or PostgreSQL (with pooling)."""
    if "sqlite" in url:
        eng = create_async_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False},
        )

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

    return create_async_engine(
        url,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=False,
    )


# Create asynchronous engine
engine = make_engine(DATABASE_URL)

# Async session factory
async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def ensure_database_exists(url: str) -> None:
    """Ensure PostgreSQL target database exists by creating it via maintenance db if needed."""
    parsed_url = make_url(url)
    target_db = parsed_url.database
    if not target_db or target_db in ("postgres", "template1"):
        return

    maint_url = parsed_url.set(database="postgres")
    maint_engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    try:
        async with maint_engine.connect() as conn:
            check_stmt = text("SELECT 1 FROM pg_database WHERE datname = :name")
            result = await conn.execute(check_stmt, {"name": target_db})
            if result.scalar_one_or_none() is None:
                await conn.execute(text(f'CREATE DATABASE "{target_db}"'))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Could not auto-create postgres database '%s': %s", target_db, exc)
    finally:
        await maint_engine.dispose()


async def init_db(bind: AsyncEngine = engine) -> None:
    """Initialize database tables for Course Catalog Service."""
    if "postgresql" in str(bind.url):
        await ensure_database_exists(str(bind.url))

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
