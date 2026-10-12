"""
student_service/main.py
Student Profile & Academic Standing Service (Port 8001).

Responsibilities:
  - Student profiles, GPA, academic standing, and transcript exploration
  - Pre-flight eligibility verification for Enrollment Orchestrator (Mediator)
  - Credential verification endpoint for API Gateway JWT login
  - Financial hold management and credit tally updates
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.schemas import (
    AcademicStanding,
    StudentDTO,
    StudentEligibilityResponse,
)
from student_service.database import get_db, init_db
from student_service.models import Student, hash_password
from student_service.schemas import (
    CompletedCoursesUpdateRequest,
    CredentialVerifyRequest,
    CredentialVerifyResponse,
    EnrolledCreditsUpdateRequest,
    FinancialHoldUpdateRequest,
    StudentCreateRequest,
    StudentProfileDetailResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup and shutdown."""
    await init_db()
    yield


app = FastAPI(
    title="Student Profile Service",
    description="Manages student profiles, GPA, academic standing, and eligibility.",
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
        "service": "student_service",
        "status": "healthy",
        "port": 8001,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Student Profile Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }


# ---------------------------------------------------------
# Student Exploration & Profiles
# ---------------------------------------------------------


@app.get(
    "/students",
    response_model=list[StudentProfileDetailResponse],
    tags=["Students"],
)
async def list_students(
    standing: AcademicStanding | None = Query(
        None, description="Filter by academic standing"
    ),
    has_hold: bool | None = Query(
        None, description="Filter by financial hold status"
    ),
    search: str | None = Query(
        None, description="Search by student name, ID, or email"
    ),
    session: AsyncSession = Depends(get_db),
):
    """List students with optional academic standing, hold, and search filters."""
    stmt = select(Student)

    if standing:
        stmt = stmt.where(Student.academic_standing_raw == standing.value)

    if has_hold is not None:
        stmt = stmt.where(Student.financial_hold == has_hold)

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Student.id.ilike(pattern),
                Student.name.ilike(pattern),
                Student.email.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Student.id)
    result = await session.execute(stmt)
    students = result.scalars().all()

    return [
        StudentProfileDetailResponse(
            id=s.id,
            name=s.name,
            email=s.email,
            major=s.major,
            year=s.year,
            gpa=s.gpa,
            academic_standing=s.academic_standing,
            financial_hold=s.financial_hold,
            completed_courses=s.completed_courses,
            enrolled_credits=s.enrolled_credits,
            max_credits=s.max_credits,
        )
        for s in students
    ]


@app.get(
    "/students/{id}",
    response_model=StudentProfileDetailResponse,
    tags=["Students"],
)
async def get_student(id: str, session: AsyncSession = Depends(get_db)):
    """Retrieve full student profile, academic standing, and completed courses."""
    norm_id = id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student '{norm_id}' not found.",
        )

    return StudentProfileDetailResponse(
        id=student.id,
        name=student.name,
        email=student.email,
        major=student.major,
        year=student.year,
        gpa=student.gpa,
        academic_standing=student.academic_standing,
        financial_hold=student.financial_hold,
        completed_courses=student.completed_courses,
        enrolled_credits=student.enrolled_credits,
        max_credits=student.max_credits,
    )


# ---------------------------------------------------------
# Eligibility Verification (Enrollment Orchestrator Mediator)
# ---------------------------------------------------------


@app.get(
    "/students/{id}/eligibility",
    response_model=StudentEligibilityResponse,
    tags=["Eligibility"],
)
async def check_eligibility(id: str, session: AsyncSession = Depends(get_db)):
    """Evaluate student registration eligibility and remaining credit load.

    Used by Enrollment Orchestrator during pre-flight registration validation.
    Enforces financial hold blocks, suspension locks, and probation credit caps.
    """
    norm_id = id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student '{norm_id}' not found.",
        )

    # 1. Financial Hold Check
    if student.financial_hold:
        return StudentEligibilityResponse(
            student_id=student.id,
            is_eligible=False,
            reason="Financial hold active on account. Clear outstanding dues before registering.",
            remaining_credits=0,
        )

    # 2. Academic Suspension Check
    if student.academic_standing == AcademicStanding.SUSPENDED:
        return StudentEligibilityResponse(
            student_id=student.id,
            is_eligible=False,
            reason="Student is under academic suspension.",
            remaining_credits=0,
        )

    # 3. Academic Probation Credit Ceiling (15 credits limit)
    effective_max_credits = student.max_credits
    if student.academic_standing == AcademicStanding.PROBATION:
        effective_max_credits = min(student.max_credits, 15)

    if student.enrolled_credits >= effective_max_credits:
        return StudentEligibilityResponse(
            student_id=student.id,
            is_eligible=False,
            reason=(
                f"Maximum allowed credit limit of {effective_max_credits} credits reached."
            ),
            remaining_credits=0,
        )

    remaining_credits = max(0, effective_max_credits - student.enrolled_credits)
    return StudentEligibilityResponse(
        student_id=student.id,
        is_eligible=True,
        reason=None,
        remaining_credits=remaining_credits,
    )


# ---------------------------------------------------------
# Credential Verification (API Gateway Login)
# ---------------------------------------------------------


@app.post(
    "/students/verify-credentials",
    response_model=CredentialVerifyResponse,
    tags=["Authentication"],
)
async def verify_credentials(
    payload: CredentialVerifyRequest,
    session: AsyncSession = Depends(get_db),
):
    """Verify student login credentials.

    Called internally by API Gateway during POST /auth/login.
    Does not issue tokens; returns validity boolean and user metadata.
    """
    norm_id = payload.student_id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student or not student.check_password(payload.password):
        return CredentialVerifyResponse(
            valid=False,
            student_id=norm_id,
            role="STUDENT",
            name=None,
            message="Invalid student ID or password.",
        )

    return CredentialVerifyResponse(
        valid=True,
        student_id=student.id,
        role="STUDENT",
        name=student.name,
        message="Credentials successfully verified.",
    )


# ---------------------------------------------------------
# Administration & Updates
# ---------------------------------------------------------


@app.post(
    "/students",
    response_model=StudentProfileDetailResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Students", "Admin"],
)
async def create_student(
    payload: StudentCreateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Register a new student profile in the student database."""
    norm_id = payload.id.strip().upper()
    existing = await session.execute(
        select(Student).where(Student.id == norm_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Student with ID '{norm_id}' already exists.",
        )

    email_norm = payload.email.strip().lower()
    existing_email = await session.execute(
        select(Student).where(Student.email == email_norm)
    )
    if existing_email.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{email_norm}' already in use.",
        )

    new_student = Student(
        id=norm_id,
        name=payload.name.strip(),
        email=email_norm,
        major=payload.major.strip(),
        year=payload.year,
        gpa=payload.gpa,
        academic_standing_raw=payload.academic_standing.value,
        financial_hold=payload.financial_hold,
        completed_courses=payload.completed_courses,
        enrolled_credits=payload.enrolled_credits,
        max_credits=payload.max_credits,
        password_hash=hash_password(payload.password),
    )
    session.add(new_student)
    await session.commit()
    await session.refresh(new_student)

    return StudentProfileDetailResponse(
        id=new_student.id,
        name=new_student.name,
        email=new_student.email,
        major=new_student.major,
        year=new_student.year,
        gpa=new_student.gpa,
        academic_standing=new_student.academic_standing,
        financial_hold=new_student.financial_hold,
        completed_courses=new_student.completed_courses,
        enrolled_credits=new_student.enrolled_credits,
        max_credits=new_student.max_credits,
    )


@app.patch(
    "/students/{id}/hold",
    response_model=StudentDTO,
    tags=["Students", "Admin"],
)
async def update_financial_hold(
    id: str,
    payload: FinancialHoldUpdateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Place or release a financial hold on a student account."""
    norm_id = id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student '{norm_id}' not found.",
        )

    student.financial_hold = payload.financial_hold
    await session.commit()
    await session.refresh(student)

    return student.to_dto()


@app.patch(
    "/students/{id}/credits",
    response_model=StudentDTO,
    tags=["Students", "Internal"],
)
async def update_enrolled_credits(
    id: str,
    payload: EnrolledCreditsUpdateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Update student's current enrolled credits tally."""
    norm_id = id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student '{norm_id}' not found.",
        )

    student.enrolled_credits = payload.enrolled_credits
    await session.commit()
    await session.refresh(student)

    return student.to_dto()


@app.patch(
    "/students/{id}/completed-courses",
    response_model=StudentDTO,
    tags=["Students", "Admin"],
)
async def update_completed_courses(
    id: str,
    payload: CompletedCoursesUpdateRequest,
    session: AsyncSession = Depends(get_db),
):
    """Update student's completed transcript courses."""
    norm_id = id.strip().upper()
    stmt = select(Student).where(Student.id == norm_id)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student '{norm_id}' not found.",
        )

    student.completed_courses = payload.completed_courses
    await session.commit()
    await session.refresh(student)

    return student.to_dto()
