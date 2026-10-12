"""
student_service/seed.py
Student cohort database seeder for Student Profile Service.

Usage:
  uv run python -m student_service.seed
"""
import asyncio

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from student_service.database import async_session_maker, init_db
from student_service.models import Student, hash_password

STUDENTS_DATA = [
    {
        "id": "STU_001",
        "name": "Alice Smith",
        "email": "alice.smith@university.edu",
        "major": "Computer Engineering",
        "year": 4,
        "gpa": 3.85,
        "academic_standing": "GOOD_STANDING",
        "financial_hold": False,
        "completed_courses": ["CS101", "CS102", "CS201", "CS202"],
        "enrolled_credits": 0,
        "max_credits": 22,
        "password": "password123",
    },
    {
        "id": "STU_002",
        "name": "Bob Jones",
        "email": "bob.jones@university.edu",
        "major": "Computer Engineering",
        "year": 2,
        "gpa": 2.95,
        "academic_standing": "GOOD_STANDING",
        "financial_hold": False,
        "completed_courses": ["CS101"],
        "enrolled_credits": 0,
        "max_credits": 22,
        "password": "password123",
    },
    {
        "id": "STU_003",
        "name": "Charlie Brown",
        "email": "charlie.brown@university.edu",
        "major": "Computer Engineering",
        "year": 1,
        "gpa": 3.10,
        "academic_standing": "GOOD_STANDING",
        "financial_hold": False,
        "completed_courses": [],
        "enrolled_credits": 0,
        "max_credits": 22,
        "password": "password123",
    },
    {
        "id": "STU_004",
        "name": "Diana Prince",
        "email": "diana.prince@university.edu",
        "major": "Computer Engineering",
        "year": 3,
        "gpa": 1.85,
        "academic_standing": "PROBATION",
        "financial_hold": False,
        "completed_courses": ["CS101", "CS102"],
        "enrolled_credits": 0,
        "max_credits": 15,  # Capped due to academic probation
        "password": "password123",
    },
    {
        "id": "STU_005",
        "name": "Evan Wright",
        "email": "evan.wright@university.edu",
        "major": "Computer Engineering",
        "year": 4,
        "gpa": 3.20,
        "academic_standing": "GOOD_STANDING",
        "financial_hold": True,  # Financial hold active
        "completed_courses": ["CS101", "CS102"],
        "enrolled_credits": 0,
        "max_credits": 22,
        "password": "password123",
    },
    {
        "id": "STU_006",
        "name": "Fiona Gallagher",
        "email": "fiona.gallagher@university.edu",
        "major": "Computer Engineering",
        "year": 2,
        "gpa": 1.45,
        "academic_standing": "SUSPENDED",  # Suspended
        "financial_hold": False,
        "completed_courses": ["CS101"],
        "enrolled_credits": 0,
        "max_credits": 0,
        "password": "password123",
    },
    {
        "id": "STU_007",
        "name": "George Clark",
        "email": "george.clark@university.edu",
        "major": "Computer Engineering",
        "year": 4,
        "gpa": 3.60,
        "academic_standing": "GOOD_STANDING",
        "financial_hold": False,
        "completed_courses": ["CS101", "CS102", "CS201", "CS202", "CS301"],
        "enrolled_credits": 22,  # Already at maximum credit capacity
        "max_credits": 22,
        "password": "password123",
    },
]


async def seed_students(session: AsyncSession) -> dict[str, int]:
    """Idempotently seed student records into student.db."""
    # Clean existing records
    await session.execute(delete(Student))
    await session.commit()

    created_count = 0
    for data in STUDENTS_DATA:
        student = Student(
            id=data["id"],
            name=data["name"],
            email=data["email"],
            major=data["major"],
            year=data["year"],
            gpa=data["gpa"],
            academic_standing_raw=data["academic_standing"],
            financial_hold=data["financial_hold"],
            completed_courses=data["completed_courses"],
            enrolled_credits=data["enrolled_credits"],
            max_credits=data["max_credits"],
            password_hash=hash_password(data["password"]),
        )
        session.add(student)
        created_count += 1

    await session.commit()
    return {"students_seeded": created_count}


async def main() -> None:
    print("[Seed] Initializing student database schema...")
    await init_db()
    async with async_session_maker() as session:
        result = await seed_students(session)
        print(f"[Seed] Successfully seeded {result['students_seeded']} student profiles!")


if __name__ == "__main__":
    asyncio.run(main())
