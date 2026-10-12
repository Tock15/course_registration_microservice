"""
tests/test_student_database.py
Unit tests verifying Student Profile Service database models,
password hashing security, and Pydantic DTO serialization.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common.schemas import AcademicStanding, StudentDTO
from student_service.models import Base, Student, hash_password, verify_password


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
async def test_password_hashing_and_verification():
    """Verify PBKDF2 salt and hash generation and validation."""
    pwd = "secret_password_2026"
    hashed = hash_password(pwd)

    assert "$" in hashed
    assert verify_password(pwd, hashed) is True
    assert verify_password("wrong_password", hashed) is False
    assert verify_password(pwd, "invalid_hash_string") is False


@pytest.mark.asyncio
async def test_create_student_and_dto_mapping(async_test_session: AsyncSession):
    """Verify Student ORM model insertion and DTO conversion."""
    session = async_test_session

    student = Student(
        id="STU_999",
        name="Test Student",
        email="test.student@kmitl.ac.th",
        major="Computer Engineering",
        year=3,
        gpa=3.75,
        academic_standing_raw="GOOD_STANDING",
        financial_hold=False,
        completed_courses=["CS101", "CS102"],
        enrolled_credits=6,
        max_credits=22,
        password_hash=hash_password("mypassword"),
    )
    session.add(student)
    await session.commit()

    stmt = select(Student).where(Student.id == "STU_999")
    result = await session.execute(stmt)
    retrieved = result.scalar_one()

    assert retrieved.id == "STU_999"
    assert retrieved.name == "Test Student"
    assert retrieved.gpa == 3.75
    assert retrieved.academic_standing == AcademicStanding.GOOD_STANDING
    assert retrieved.completed_courses == ["CS101", "CS102"]
    assert retrieved.check_password("mypassword") is True
    assert retrieved.check_password("wrong") is False

    # Test DTO serialization
    dto = retrieved.to_dto()
    assert isinstance(dto, StudentDTO)
    assert dto.id == "STU_999"
    assert dto.completed_courses == ["CS101", "CS102"]
    assert dto.academic_standing == AcademicStanding.GOOD_STANDING
