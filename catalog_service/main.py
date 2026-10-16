"""
catalog_service/main.py
Course Catalog & Prerequisite DAG Service (Port 8002).

Responsibilities:
  - Course & Section exploration with multi-criteria filtering (FR-01, FR-02)
  - Prerequisite Directed Acyclic Graph (DAG) validation (FR-03)
  - Dynamic quota expansion telemetry (FR-14)
  - Curriculum and Section administration (FR-13)
  - Idempotent seat reservation & release protocol (ADR-001)
"""
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from catalog_service.database import async_session_maker, get_db, init_db
from catalog_service.models import Course, Prerequisite, SeatReservation, Section
from catalog_service.prereq_dag import PrereqDAG, build_dag_from_db
from catalog_service.schemas import (
    CapacityUpdateRequest,
    CourseCreateRequest,
    SeatReleaseRequest,
    SeatReleaseResponse,
    SeatReserveRequest,
    SeatReserveResponse,
    SectionCreateRequest,
)
from catalog_service.seed import seed_catalog_data
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
    async with async_session_maker() as session:
        result = await session.execute(select(Course.code).limit(1))
        if result.scalar_one_or_none() is None:
            await seed_catalog_data(session)
        app.state.dag = await build_dag_from_db(session)
    yield


app = FastAPI(
    title="Course Catalog Service",
    description="Manages course offerings, sections, quotas, and prerequisite graph traversal.",
    version="0.1.0",
    lifespan=lifespan,
)


async def get_or_build_dag(app: FastAPI, session: AsyncSession) -> PrereqDAG:
    """Retrieve in-memory PrereqDAG or lazily initialize from database."""
    dag: PrereqDAG | None = getattr(app.state, "dag", None)
    if dag is None:
        dag = await build_dag_from_db(session)
        app.state.dag = dag
    return dag


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


@app.post("/seed", tags=["Admin"])
@app.post("/reset", tags=["Admin"])
async def reset_catalog(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Reset and reseed course catalog data (courses, prerequisites, sections)."""
    if os.getenv("ALLOW_RESET", "false").lower() not in ("true", "1"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reset endpoint is disabled in this environment.",
        )
    stats = await seed_catalog_data(session)
    request.app.state.dag = None
    return {"ok": True, "details": stats}


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
    request: Request,
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

    # 2. Check prerequisites existence & circular dependencies using in-memory DAG
    current_dag = await get_or_build_dag(request.app, session)
    dag = current_dag.copy()
    dag.add_course(norm_code)

    prereq_records: list[Prerequisite] = []
    for req in payload.prerequisites:
        req_norm = req.strip().upper()
        # Verify required course exists
        if req_norm not in dag.adj:
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

    # Invalidate in-memory DAG cache so next request rebuilds from database
    request.app.state.dag = None

    return CourseDTO.model_validate(new_course)


# ---------------------------------------------------------
# Section Endpoints (FR-01, FR-02, FR-13, FR-14)
# ---------------------------------------------------------


@app.get("/sections", response_model=list[SectionDTO], tags=["Sections"])
async def list_sections(
    request: Request,
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
    stmt = (
        select(Section)
        .options(joinedload(Section.course))
        .join(Course)
    )

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

    dag: PrereqDAG | None = getattr(request.app.state, "dag", None)
    dtos: list[SectionDTO] = []
    for s in sections:
        dto = SectionDTO.model_validate(s)
        if dag is not None and not dto.direct_prereqs:
            dto.direct_prereqs = dag.get_direct_prereqs(s.course_code)
        dtos.append(dto)

    return dtos


@app.get("/sections/{id}", response_model=SectionDTO, tags=["Sections"])
async def get_section(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Retrieve section metadata and live capacity telemetry."""
    stmt = (
        select(Section)
        .options(joinedload(Section.course))
        .where(Section.id == id)
    )
    result = await session.execute(stmt)
    section = result.scalar_one_or_none()

    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Section {id} not found.",
        )

    dto = SectionDTO.model_validate(section)
    dag: PrereqDAG | None = getattr(request.app.state, "dag", None)
    if dag is not None and not dto.direct_prereqs:
        dto.direct_prereqs = dag.get_direct_prereqs(section.course_code)
    return dto


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
# Seat Reservation Protocol Endpoints (ADR-001)
# ---------------------------------------------------------


@app.post(
    "/sections/{id}/seats/reserve",
    response_model=SeatReserveResponse,
    responses={
        409: {
            "model": SeatReserveResponse,
            "description": "Section full or reservation conflict",
        },
        404: {"description": "Section not found"},
    },
    tags=["Sections", "Seats"],
)
async def reserve_seat(
    id: int,
    payload: SeatReserveRequest,
    session: AsyncSession = Depends(get_db),
):
    """Atomically reserve a seat in a section using an idempotent seat key (ADR-001).

    Guarantees:
      - Atomic conditional SQL update ensures zero overbooking (enrolled_count < capacity).
      - Replaying with the same seat_key returns the existing active reservation idempotently.
      - Returns HTTP 409 FULL when section capacity is exhausted.
    """
    seat_key = payload.seat_key.strip()

    # 1. Check for existing reservation with this seat_key
    existing_stmt = select(SeatReservation).where(SeatReservation.seat_key == seat_key)
    existing_res = (await session.execute(existing_stmt)).scalar_one_or_none()

    if existing_res is not None:
        if existing_res.section_id != id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Seat key '{seat_key}' was used for section {existing_res.section_id}.",
            )
        if existing_res.status == "ACTIVE":
            return SeatReserveResponse(
                reserved=True,
                seat_key=seat_key,
                detail="Seat already reserved.",
            )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reserved": False,
                "seat_key": seat_key,
                "reason": existing_res.status,
                "detail": f"Seat reservation has already been {existing_res.status.lower()}.",
            },
        )

    # 2. Atomic conditional update to allocate seat (Zero Overbooking Invariant)
    alloc_stmt = (
        update(Section)
        .where(Section.id == id, Section.enrolled_count < Section.capacity)
        .values(enrolled_count=Section.enrolled_count + 1)
    )
    result = await session.execute(alloc_stmt)

    if result.rowcount == 0:
        section = await session.get(Section, id)
        if not section:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Section {id} not found.",
            )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reserved": False,
                "seat_key": seat_key,
                "reason": "FULL",
                "detail": f"Section {id} is at maximum capacity ({section.capacity}).",
            },
        )

    # 4. Insert active reservation record
    reservation = SeatReservation(
        section_id=id,
        seat_key=seat_key,
        status="ACTIVE",
    )
    session.add(reservation)
    try:
        await session.commit()
    except IntegrityError:
        # Race condition on identical seat_key commit
        await session.rollback()
        winner_stmt = select(SeatReservation).where(SeatReservation.seat_key == seat_key)
        winner = (await session.execute(winner_stmt)).scalar_one_or_none()
        if winner and winner.section_id == id and winner.status == "ACTIVE":
            return SeatReserveResponse(reserved=True, seat_key=seat_key)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reserved": False,
                "seat_key": seat_key,
                "reason": "CONFLICT",
                "detail": "Seat reservation conflict.",
            },
        )

    return SeatReserveResponse(reserved=True, seat_key=seat_key)


@app.post(
    "/sections/{id}/seats/release",
    response_model=SeatReleaseResponse,
    responses={
        404: {"description": "Section not found"},
    },
    tags=["Sections", "Seats"],
)
async def release_seat(
    id: int,
    payload: SeatReleaseRequest,
    session: AsyncSession = Depends(get_db),
):
    """Release a reserved seat in a section using idempotent seat key (ADR-001).

    Guarantees:
      - Atomically decrements enrolled_count if reservation was ACTIVE.
      - Idempotent: repeated calls do not decrement enrolled_count multiple times.
      - Releasing an unknown seat key marks it CANCELLED to prevent late reservation races.
    """
    seat_key = payload.seat_key.strip()

    # 1. Verify section exists
    section = await session.get(Section, id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Section {id} not found.",
        )

    # 2. Check for reservation record
    existing_stmt = select(SeatReservation).where(SeatReservation.seat_key == seat_key)
    res_record = (await session.execute(existing_stmt)).scalar_one_or_none()

    if res_record is None:
        cancelled_res = SeatReservation(
            section_id=id,
            seat_key=seat_key,
            status="CANCELLED",
        )
        session.add(cancelled_res)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        return SeatReleaseResponse(released=True, seat_key=seat_key, reason="CANCELLED")

    if res_record.section_id != id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Seat key was assigned to section {res_record.section_id}, not {id}.",
        )

    if res_record.status == "ACTIVE":
        dec_stmt = (
            update(Section)
            .where(Section.id == id, Section.enrolled_count > 0)
            .values(enrolled_count=Section.enrolled_count - 1)
        )
        await session.execute(dec_stmt)
        res_record.status = "RELEASED"
        await session.commit()
        return SeatReleaseResponse(released=True, seat_key=seat_key)

    # Already RELEASED or CANCELLED
    return SeatReleaseResponse(released=True, seat_key=seat_key, reason=res_record.status)


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
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Validate student eligibility against prerequisite DAG.

    Dual-mode:
      - Uses completed_courses if provided in payload.
      - Fallback: queries student_service to retrieve completed transcript courses.
    """
    norm_code = payload.course_code.strip().upper()
    dag = await get_or_build_dag(request.app, session)

    # Verify target course exists directly from in-memory DAG
    if norm_code not in dag.adj:
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

    # Serve directly from the in-memory DAG
    return dag.validate_student(
        course_code=norm_code,
        completed_courses=completed_courses,
        transitive=False,
    )
