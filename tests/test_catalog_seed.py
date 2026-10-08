"""
tests/test_catalog_seed.py
Verifies that seed_catalog_data runs idempotently and populates expected
curriculum and stress test sections.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalog_service.models import Base, Course, Section
from catalog_service.prereq_dag import build_dag_from_db
from catalog_service.seed import seed_catalog_data


@pytest.mark.asyncio
async def test_seed_catalog_data_idempotency():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        # First seed
        stats1 = await seed_catalog_data(session)
        assert stats1["courses"] >= 10
        assert stats1["sections"] >= 7

        # Second seed (idempotent overwrite)
        stats2 = await seed_catalog_data(session)
        assert stats2["courses"] == stats1["courses"]
        assert stats2["sections"] == stats1["sections"]

        # Verify Section 101 stress test target
        sec101_query = await session.execute(select(Section).where(Section.id == 101))
        sec101 = sec101_query.scalar_one_or_none()
        assert sec101 is not None
        assert sec101.course_code == "CS301"
        assert sec101.capacity == 10
        assert sec101.schedule_days == ["MON", "WED"]

        # Verify CS301 prerequisite diamond in DAG
        dag = await build_dag_from_db(session)
        assert "CS102" in dag.get_direct_prereqs("CS301")
        assert "CS202" in dag.get_direct_prereqs("CS301")
        assert dag.get_all_prereqs("CS301") == {"CS102", "CS202", "CS101"}

        # Verify course count
        courses_query = await session.execute(select(Course))
        all_courses = courses_query.scalars().all()
        assert len(all_courses) == stats1["courses"]

    await engine.dispose()
