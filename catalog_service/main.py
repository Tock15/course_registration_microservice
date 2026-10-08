"""
catalog_service/main.py
Course Catalog & Prerequisite DAG Service (Port 8002).

Responsibilities:
  - Course & Section exploration with multi-criteria filtering (FR-01, FR-02)
  - Prerequisite Directed Acyclic Graph (DAG) validation (FR-03)
  - Dynamic quota expansion telemetry (FR-14)
  - Curriculum and Section administration (FR-13)
"""
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from catalog_service.database import get_db, init_db
from catalog_service.models import Course, Prerequisite, Section
from catalog_service.prereq_dag import build_dag_from_db
from catalog_service.schemas import (
    CapacityUpdateRequest,
    CourseCreateRequest,
    SectionCreateRequest,
)
from common.schemas import (
    CourseDTO,
    PrereqValidationRequest,
    PrereqValidationResponse,
    SectionDTO,
)

STUDENT_SERVICE_URL = os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup and shutdown."""
    # Ensure database schema is initialized on startup
    await init_db()
    yield


app = FastAPI(
    title="Course Catalog Service",
    description="Manages course offerings, sections, quotas, and prerequisite graph traversal.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------
# Health & Service Information
# ---------------------------------------------------------


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "catalog_service",
        "status": "healthy",
        "port": 8002,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Course Catalog Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }


# ---------------------------------------------------------
# Course Catalog Endpoints (FR-01, FR-13)
# ---------------------------------------------------------


@app.get("/courses", response_model=list[CourseDTO], tags=["Courses"])
async def list_courses(
    department: str | None = Query(None, description="Filter by department"),
    search: str | None = Query(None, description="Search keyword in code or title"),
    session: AsyncSession = Depends(get_db),
):
    """List courses with optional department and keyword filtering."""
    stmt = select(Course).options(selectinload(Course.prerequisite_records))

    if department:
        stmt = stmt.where(Course.department.ilike(f"%{department.strip()}%"))

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Course.code.ilike(pattern),
                Course.title.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Course.code)
    result = await session.execute(stmt)
    courses = result.scalars().all()

    return [CourseDTO.model_validate(c) for c in courses]


@app.get("/courses/{code}", response_model=CourseDTO, tags=["Courses"])
async def get_course(code: str, session: AsyncSession = Depends(get_db)):
    """Retrieve course details including prerequisite codes."""
    norm_code = code.strip().upper()
    stmt = (
        select(Course)
        .options(selectinload(Course.prerequisite_records))
        .where(Course.code == norm_code)
    )
    result = await session.execute(stmt)
    course = result.scalar_one_or_none()

    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course '{norm_code}' not found.",
        )

    return CourseDTO.model_validate(course)


@app.post(
    "/courses",
    response_model=CourseDTO,
    status_code=status.HTTP_201_CREATED,
    tags=["Courses", "Admin"],
)
async def create_course(
    payload: CourseCreateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Create a new course offering and configure its prerequisites.

    Validates that newly assigned prerequisites do not create cyclic dependencies.
    """
    norm_code = payload.code.strip().upper()

    # 1. Check for existing course code
    existing = await session.execute(
        select(Course).where(Course.code == norm_code)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Course '{norm_code}' already exists.",
        )

    # 2. Check prerequisites existence & circular dependencies
    dag = await build_dag_from_db(session)
    dag.add_course(norm_code)

    prereq_records: list[Prerequisite] = []
    for req in payload.prerequisites:
        req_norm = req.strip().upper()
        # Verify required course exists
        req_check = await session.execute(
            select(Course).where(Course.code == req_norm)
        )
        if not req_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Prerequisite course '{req_norm}' does not exist in catalog.",
            )

        # Check for circular dependency
        if dag.would_create_cycle(norm_code, req_norm):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Adding prerequisite '{req_norm}' to '{norm_code}' would create a cycle.",
            )

        dag.add_prerequisite(norm_code, req_norm)
        prereq_records.append(
            Prerequisite(
                course_code=norm_code,
                required_course_code=req_norm,
                min_grade_required="D",
            )
        )

    # 3. Create course and attach prerequisites
    new_course = Course(
        code=norm_code,
        title=payload.title.strip(),
        description=payload.description,
        credits=payload.credits,
        department=payload.department.strip(),
        is_elective=payload.is_elective,
        prerequisite_records=prereq_records,
    )
    session.add(new_course)
    await session.commit()
    await session.refresh(new_course)

    return CourseDTO.model_validate(new_course)


# ---------------------------------------------------------
# Section Endpoints (FR-01, FR-02, FR-13, FR-14)
# ---------------------------------------------------------


@app.get("/sections", response_model=list[SectionDTO], tags=["Sections"])
async def list_sections(
    course_code: str | None = Query(None, description="Course code filter"),
    department: str | None = Query(None, description="Department filter"),
    day: str | None = Query(None, description="Schedule day, e.g. MON"),
    instructor: str | None = Query(None, description="Instructor name filter"),
    open_only: bool = Query(
        False, description="Filter only sections with available seats"
    ),
    session: AsyncSession = Depends(get_db),
):
    """List sections offering live capacity telemetry and multi-criteria filtering."""
    stmt = select(Section).join(Course)

    if course_code:
        stmt = stmt.where(Section.course_code == course_code.strip().upper())

    if department:
        stmt = stmt.where(Course.department.ilike(f"%{department.strip()}%"))

    if instructor:
        stmt = stmt.where(Section.instructor.ilike(f"%{instructor.strip()}%"))

    if open_only:
        stmt = stmt.where(Section.enrolled_count < Section.capacity)

    stmt = stmt.order_by(Section.course_code, Section.section_number)
    result = await session.execute(stmt)
    sections = result.scalars().all()

    # Filter by day in schedule_days if requested
    if day:
        day_norm = day.strip().upper()
        sections = [
            s
            for s in sections
            if day_norm in [d.upper() for d in s.schedule_days]
        ]

    return [SectionDTO.model_validate(s) for s in sections]


@app.get("/sections/{id}", response_model=SectionDTO, tags=["Sections"])
async def get_section(id: int, session: AsyncSession = Depends(get_db)):
    """Retrieve section metadata and live capacity telemetry."""
    stmt = select(Section).where(Section.id == id)
    result = await session.execute(stmt)
    section = result.scalar_one_or_none()

    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Section {id} not found.",
        )

    return SectionDTO.model_validate(section)


@app.post(
    "/sections",
    response_model=SectionDTO,
    status_code=status.HTTP_201_CREATED,
    tags=["Sections", "Admin"],
)
async def create_section(
    payload: SectionCreateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Create a new section offering for a course."""
    norm_code = payload.course_code.strip().upper()

    course_check = await session.execute(
        select(Course).where(Course.code == norm_code)
    )
    if not course_check.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course '{norm_code}' not found.",
        )

    new_section = Section(
        course_code=norm_code,
        section_number=payload.section_number,
        capacity=payload.capacity,
        enrolled_count=0,
        semester=payload.semester,
        academic_year=payload.academic_year,
        room=payload.room,
        schedule_days=payload.schedule_days,
        start_time=payload.start_time,
        end_time=payload.end_time,
        instructor=payload.instructor,
    )
    session.add(new_section)
    await session.commit()
    await session.refresh(new_section)

    return SectionDTO.model_validate(new_section)


@app.patch(
    "/sections/{id}/capacity",
    response_model=SectionDTO,
    tags=["Sections", "Admin"],
)
async def update_section_capacity(
    id: int,
    payload: CapacityUpdateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Dynamically adjust section capacity (FR-14 Dynamic Quota Expansion)."""
    stmt = select(Section).where(Section.id == id)
    result = await session.execute(stmt)
    section = result.scalar_one_or_none()

    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Section {id} not found.",
        )

    if payload.capacity < section.enrolled_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"New capacity ({payload.capacity}) cannot be lower than "
                f"current enrolled count ({section.enrolled_count})."
            ),
        )

    section.capacity = payload.capacity
    await session.commit()
    await session.refresh(section)

    return SectionDTO.model_validate(section)


# ---------------------------------------------------------
# Prerequisite Validation Engine (FR-03)
# ---------------------------------------------------------


@app.post(
    "/validate-prereqs",
    response_model=PrereqValidationResponse,
    tags=["Prerequisites"],
)
async def validate_prerequisites(
    payload: PrereqValidationRequest,
    session: AsyncSession = Depends(get_db),
):
    """Validate student eligibility against prerequisite DAG.

    Dual-mode:
      - Uses completed_courses if provided in payload.
      - Fallback: queries student_service to retrieve completed transcript courses.
    """
    norm_code = payload.course_code.strip().upper()

    # Verify target course exists
    course_check = await session.execute(
        select(Course).where(Course.code == norm_code)
    )
    if not course_check.scalar_one_or_none():
        return PrereqValidationResponse(
            is_valid=False,
            missing_prereqs=[],
            message=f"Course '{norm_code}' does not exist in catalog.",
        )

    completed_courses = list(payload.completed_courses)

    # Fallback to Student Service if completed_courses not supplied
    if not completed_courses and payload.student_id:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(
                    f"{STUDENT_SERVICE_URL}/students/{payload.student_id}"
                )
                if res.status_code == 200:
                    data = res.json()
                    completed_courses = data.get("completed_courses", [])
        except httpx.HTTPError:
            # Student service unavailable; proceed with empty course list
            completed_courses = []

    # Build live DAG and validate
    dag = await build_dag_from_db(session)
    return dag.validate_student(
        course_code=norm_code,
        completed_courses=completed_courses,
        transitive=False,
    )
