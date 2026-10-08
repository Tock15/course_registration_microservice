"""
tests/test_catalog_database.py
Unit tests verifying Catalog Service database models, schema relationships,
and Pydantic DTO serialization.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalog_service.models import Base, Course, Prerequisite, Section
from common.schemas import CourseDTO, SectionDTO


@pytest.fixture
async def async_test_session():
    """In-memory SQLite async session fixture for isolated testing."""
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
async def test_create_course_with_prerequisites_and_sections(async_test_session: AsyncSession):
    session = async_test_session

    # 1. Insert parent courses
    cs101 = Course(
        code="CS101",
        title="Programming Fundamentals",
        description="Core introduction to computer programming.",
        credits=3,
        department="Computer Engineering",
        is_elective=False,
    )
    cs102 = Course(
        code="CS102",
        title="Data Structures",
        description="Fundamental data structures and algorithms.",
        credits=3,
        department="Computer Engineering",
        is_elective=False,
    )
    session.add_all([cs101, cs102])
    await session.commit()

    # 2. Add Prerequisite: CS102 requires CS101
    prereq = Prerequisite(
        course_code="CS102",
        required_course_code="CS101",
        min_grade_required="D",
    )
    session.add(prereq)

    # 3. Add Sections
    sec1 = Section(
        course_code="CS102",
        section_number=1,
        capacity=40,
        enrolled_count=10,
        room="HM402",
        schedule_days=["MON", "WED"],
        start_time="09:00",
        end_time="10:30",
        instructor="Dr. Veera B.",
    )
    session.add(sec1)
    await session.commit()

    # 4. Query back CS102 with eager loaded relationships
    result = await session.execute(
        select(Course).where(Course.code == "CS102")
    )
    course = result.scalar_one()

    assert course.code == "CS102"
    assert course.prerequisites == ["CS101"]
    assert len(course.sections) == 1
    assert course.sections[0].schedule_days == ["MON", "WED"]

    # 5. Verify Pydantic DTO compatibility
    course_dto = CourseDTO.model_validate(course)
    assert course_dto.code == "CS102"
    assert course_dto.prerequisites == ["CS101"]

    section_dto = SectionDTO.model_validate(course.sections[0])
    assert section_dto.id == sec1.id
    assert section_dto.course_code == "CS102"
    assert section_dto.schedule_days == ["MON", "WED"]
    assert section_dto.capacity == 40
    assert section_dto.enrolled_count == 10
