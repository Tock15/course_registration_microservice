"""
catalog_service/database.py
Async SQLite database connection & session lifecycle using SQLAlchemy 2.0.
"""
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
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

# Create asynchronous engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

# Async session factory
async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Initialize database tables for Course Catalog Service."""
    async with engine.begin() as conn:
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
