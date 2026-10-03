"""
common/schemas.py
Shared Pydantic v2 DTOs and API payloads across all microservices.
"""
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------
# Enums
# ---------------------------------------------------------

class UserRole(str, Enum):
    STUDENT = "STUDENT"
    REGISTRAR = "REGISTRAR"
    INSTRUCTOR = "INSTRUCTOR"


class AcademicStanding(str, Enum):
    GOOD_STANDING = "GOOD_STANDING"
    PROBATION = "PROBATION"
    SUSPENDED = "SUSPENDED"


class EnrollmentStatus(str, Enum):
    STAGED = "STAGED"
    ENROLLED = "ENROLLED"
    WAITLISTED = "WAITLISTED"
    OFFERED = "OFFERED"
    DROPPED = "DROPPED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------
# Authentication DTOs
# ---------------------------------------------------------

class LoginRequest(BaseModel):
    student_id: str = Field(..., description="Student or staff identifier, e.g. STU_001")
    password: str = Field(..., min_length=1, description="Account password")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole = UserRole.STUDENT
    student_id: str


class TokenPayload(BaseModel):
    sub: str  # student_id
    role: str = "STUDENT"
    exp: int | None = None


# ---------------------------------------------------------
# Student Service DTOs
# ---------------------------------------------------------

class StudentDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Unique student ID, e.g. STU_001")
    name: str
    gpa: float = Field(ge=0.0, le=4.0)
    academic_standing: AcademicStanding = AcademicStanding.GOOD_STANDING
    financial_hold: bool = False
    completed_courses: list[str] = Field(
        default_factory=list, description="Course codes completed, e.g. ['CS101']"
    )
    enrolled_credits: int = Field(default=0, ge=0)
    max_credits: int = Field(default=22, ge=0)


class StudentEligibilityResponse(BaseModel):
    student_id: str
    is_eligible: bool
    reason: str | None = None
    remaining_credits: int


# ---------------------------------------------------------
# Catalog Service DTOs
# ---------------------------------------------------------

class CourseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str = Field(..., description="Course code, e.g. CS102")
    title: str
    credits: int = Field(gt=0, le=6)
    prerequisites: list[str] = Field(
        default_factory=list, description="List of required course codes"
    )


class SectionDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Unique section ID, e.g. 101")
    course_code: str
    section_number: int
    capacity: int = Field(gt=0)
    enrolled_count: int = Field(default=0, ge=0)
    schedule_days: list[str] = Field(
        default_factory=list, description="e.g. ['MON', 'WED']"
    )
    start_time: str = Field(..., description="HH:MM format, e.g. '09:00'")
    end_time: str = Field(..., description="HH:MM format, e.g. '10:30'")
    room: str = Field(default="TBA")
    instructor: str = Field(default="TBA")


class PrereqValidationRequest(BaseModel):
    student_id: str
    course_code: str
    completed_courses: list[str] = Field(default_factory=list)


class PrereqValidationResponse(BaseModel):
    is_valid: bool
    missing_prereqs: list[str] = Field(default_factory=list)
    message: str | None = None


# ---------------------------------------------------------
# Enrollment Service DTOs & Validation
# ---------------------------------------------------------

class ValidationResult(BaseModel):
    is_valid: bool
    error_message: str | None = None
    rule_name: str | None = None


class EnrollmentRequest(BaseModel):
    section_id: int
    # Optional student_id for internal inter-service forwarding
    student_id: str | None = None


class EnrollmentResponse(BaseModel):
    enrollment_id: int | None = None
    student_id: str
    section_id: int
    course_code: str | None = None
    status: EnrollmentStatus
    message: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )


class DropRequest(BaseModel):
    section_id: int
    student_id: str | None = None


class WaitlistOfferClaimRequest(BaseModel):
    offer_id: int
    section_id: int
    student_id: str | None = None


class WaitlistOfferClaimResponse(BaseModel):
    offer_id: int
    student_id: str
    section_id: int
    status: str
    message: str
