"""
tests/test_student_seed.py
Unit tests verifying Student Profile Service database seeding idempotency
and cohort data coverage.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from student_service.models import Base, Student
from student_service.seed import STUDENTS_DATA, seed_students


@pytest.fixture
async def async_test_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_maker() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_students_idempotency(async_test_session: AsyncSession):
    """Verify that seed_students runs cleanly and can be re-run without duplicate key errors."""
    session = async_test_session

    # First run
    res1 = await seed_students(session)
    assert res1["students_seeded"] == len(STUDENTS_DATA)

    count_stmt = select(Student)
    res_db1 = await session.execute(count_stmt)
    assert len(res_db1.scalars().all()) == len(STUDENTS_DATA)

    # Second run (idempotency check)
    res2 = await seed_students(session)
    assert res2["students_seeded"] == len(STUDENTS_DATA)

    res_db2 = await session.execute(count_stmt)
    assert len(res_db2.scalars().all()) == len(STUDENTS_DATA)
