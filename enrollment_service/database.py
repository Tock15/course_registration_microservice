from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from enrollment_service.config import settings
from enrollment_service.models import Base


def make_engine(
    url: str,
    pool_size: int = settings.pool_size,
    max_overflow: int = settings.max_overflow,
) -> AsyncEngine:
    """Create an async engine configured for PostgreSQL with connection pooling.

    If SQLite is used (e.g., test fixtures), SQLite-compatible options are applied.
    """
    if "sqlite" in url:
        return create_async_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False},
        )

    return create_async_engine(
        url,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )


T = TypeVar("T")


async def run_with_retry(
    fn: Callable[[], Coroutine[Any, Any, T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Execute async database operation directly without SQLite lock retry loop."""
    return await fn()


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; each use case opens one short session per transaction."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


engine = make_engine(settings.database_url)
async_session_maker = make_session_maker(engine)


async def init_db(bind: AsyncEngine = engine) -> None:
    """Initialize database tables for the Enrollment Service."""
    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency yielding the session factory (overridden in tests)."""
    return async_session_maker
