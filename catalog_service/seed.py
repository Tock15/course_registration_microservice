"""
catalog_service/seed.py
Curriculum and section database seeder for Course Catalog Service.

Usage:
  uv run python -m catalog_service.seed
"""
import asyncio

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.database import async_session_maker, init_db
from catalog_service.models import Course, Prerequisite, Section

# ---------------------------------------------------------
# Academic Catalog Dataset
# ---------------------------------------------------------

COURSES_DATA = [
    # Foundational Year 1
    {
        "code": "CS101",
        "title": "Programming Fundamentals",
        "description": "Introduction to computer programming, algorithmic logic, and Python.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "MATH101",
        "title": "Calculus for Engineers",
        "description": "Differential and integral calculus, series, and linear algebra applications.",
        "credits": 3,
        "department": "Mathematics",
        "is_elective": False,
    },
    # Intermediate Year 2
    {
        "code": "CS102",
        "title": "Data Structures & Algorithms",
        "description": "Arrays, linked lists, trees, hash tables, and algorithm efficiency analysis.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "CS202",
        "title": "Database Systems",
        "description": "Relational algebra, SQL, indexing, transaction management, and ACID semantics.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "CS201",
        "title": "Advanced Algorithms",
        "description": "Graph algorithms, greedy heuristics, dynamic programming, and NP-completeness.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    # Senior Year 3 & Architecture
    {
        "code": "CS301",
        "title": "Software Design & Architecture",
        "description": "Enterprise microservices, GoF patterns, architectural trade-offs, and concurrency.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "01076228",
        "title": "Software Design & Architecture (KMITL)",
        "description": "KMITL Computer Engineering Curriculum: Software Design & Architecture.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "CS302",
        "title": "Distributed Systems",
        "description": "RPC, consensus algorithms, partition tolerance, and cloud topologies.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "01076235",
        "title": "Distributed Systems (KMITL)",
        "description": "KMITL Computer Engineering Curriculum: Distributed Systems.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": False,
    },
    {
        "code": "CS303",
        "title": "Cloud Computing",
        "description": "Virtualization, containerization, Kubernetes, and serverless architectures.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": True,
    },
    {
        "code": "01076230",
        "title": "Cloud Computing (KMITL)",
        "description": "KMITL Computer Engineering Curriculum: Cloud Computing.",
        "credits": 3,
        "department": "Computer Engineering",
        "is_elective": True,
    },
]

PREREQUISITES_DATA = [
    # CS102 requires CS101
    ("CS102", "CS101", "D"),
    # CS202 requires CS101
    ("CS202", "CS101", "D"),
    # CS201 requires CS102
    ("CS201", "CS102", "C"),
    # CS301 requires CS102 and CS202 (Diamond Prereq Structure)
    ("CS301", "CS102", "D"),
    ("CS301", "CS202", "D"),
    # 01076228 requires CS102 and CS202
    ("01076228", "CS102", "D"),
    ("01076228", "CS202", "D"),
    # CS302 requires CS201 and CS202
    ("CS302", "CS201", "D"),
    ("CS302", "CS202", "D"),
    ("01076235", "CS201", "D"),
    ("01076235", "CS202", "D"),
    # Cloud Computing requires CS202
    ("CS303", "CS202", "D"),
    ("01076230", "CS202", "D"),
]

SECTIONS_DATA = [
    # Section 101: Stress-Test Target (Capacity 10, Zero-Overbooking benchmark)
    {
        "id": 101,
        "course_code": "CS301",
        "section_number": 1,
        "capacity": 10,
        "enrolled_count": 0,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "HM402",
        "schedule_days": ["MON", "WED"],
        "start_time": "09:00",
        "end_time": "10:30",
        "instructor": "Dr. Veera B.",
    },
    # Section 102: CS301 Lecture Section 2
    {
        "id": 102,
        "course_code": "CS301",
        "section_number": 2,
        "capacity": 50,
        "enrolled_count": 48,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "HM402",
        "schedule_days": ["MON"],
        "start_time": "13:00",
        "end_time": "16:00",
        "instructor": "Dr. Veera B.",
    },
    # Section 103: CS101 Large Lecture
    {
        "id": 103,
        "course_code": "CS101",
        "section_number": 1,
        "capacity": 60,
        "enrolled_count": 15,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "ECC801",
        "schedule_days": ["TUE", "THU"],
        "start_time": "09:00",
        "end_time": "10:30",
        "instructor": "Faculty Staff",
    },
    # Section 104: CS102 Section 1
    {
        "id": 104,
        "course_code": "CS102",
        "section_number": 1,
        "capacity": 45,
        "enrolled_count": 20,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "ECC704",
        "schedule_days": ["MON", "WED"],
        "start_time": "13:00",
        "end_time": "14:30",
        "instructor": "Faculty Staff",
    },
    # Section 105: CS202 Lab Offering
    {
        "id": 105,
        "course_code": "CS202",
        "section_number": 1,
        "capacity": 40,
        "enrolled_count": 35,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "HM402",
        "schedule_days": ["FRI"],
        "start_time": "09:00",
        "end_time": "12:00",
        "instructor": "Faculty Staff",
    },
    # Section 106: Cloud Computing 01076230
    {
        "id": 106,
        "course_code": "01076230",
        "section_number": 1,
        "capacity": 40,
        "enrolled_count": 32,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "ECC801",
        "schedule_days": ["WED"],
        "start_time": "09:00",
        "end_time": "12:00",
        "instructor": "Faculty Staff",
    },
    # Section 107: Distributed Systems 01076235
    {
        "id": 107,
        "course_code": "01076235",
        "section_number": 1,
        "capacity": 30,
        "enrolled_count": 25,
        "semester": "2026/1",
        "academic_year": 2026,
        "room": "ECC704",
        "schedule_days": ["TUE"],
        "start_time": "13:00",
        "end_time": "16:00",
        "instructor": "Faculty Staff",
    },
]


async def seed_catalog_data(session: AsyncSession) -> dict[str, int]:
    """Populate courses, prerequisites, and sections idempotently."""
    # 1. Clean existing records in reverse dependency order
    await session.execute(delete(Section))
    await session.execute(delete(Prerequisite))
    await session.execute(delete(Course))
    await session.commit()

    # 2. Insert Courses
    courses = [Course(**c) for c in COURSES_DATA]
    session.add_all(courses)
    await session.commit()

    # 3. Insert Prerequisites
    prereqs = [
        Prerequisite(
            course_code=c,
            required_course_code=req,
            min_grade_required=grade,
        )
        for c, req, grade in PREREQUISITES_DATA
    ]
    session.add_all(prereqs)
    await session.commit()

    # 4. Insert Sections
    sections = []
    for s_data in SECTIONS_DATA:
        data = dict(s_data)
        days = data.pop("schedule_days")
        sec = Section(**data)
        sec.schedule_days = days
        sections.append(sec)

    session.add_all(sections)
    await session.commit()

    return {
        "courses": len(courses),
        "prerequisites": len(prereqs),
        "sections": len(sections),
    }


async def main() -> None:
    """CLI runner for database seeding."""
    print("=" * 60)
    print("  Bootstrapping Course Catalog Database (catalog.db)...")
    print("=" * 60)

    await init_db()

    async with async_session_maker() as session:
        stats = await seed_catalog_data(session)

    print(f"[OK] Seeded {stats['courses']} Courses.")
    print(f"[OK] Seeded {stats['prerequisites']} Prerequisite rules.")
    print(f"[OK] Seeded {stats['sections']} Scheduled Sections.")
    print("-" * 60)
    print("  Highlight:")
    print("  - Section 101: CS301 (Capacity: 10) [Stress-Test Target]")
    print("  - CS301 Prereq Diamond: CS301 -> (CS102, CS202) -> CS101")
    print("=" * 60)
    print("Catalog seeding complete!")


if __name__ == "__main__":
    asyncio.run(main())
