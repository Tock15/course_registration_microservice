"""
student_service/schemas.py
Service-specific Pydantic schemas for student profile administration and credential verification.
"""
from pydantic import BaseModel, Field

from common.schemas import AcademicStanding, StudentDTO


class StudentCreateRequest(BaseModel):
    id: str = Field(..., description="Unique student ID, e.g. STU_001")
    name: str = Field(..., min_length=2, description="Full student name")
    email: str = Field(..., description="Institutional email address")
    password: str = Field(..., min_length=6, description="Plaintext initial password")
    major: str = Field(default="Computer Engineering")
    year: int = Field(default=1, ge=1, le=8)
    gpa: float = Field(default=3.00, ge=0.0, le=4.0)
    academic_standing: AcademicStanding = AcademicStanding.GOOD_STANDING
    financial_hold: bool = False
    completed_courses: list[str] = Field(default_factory=list)
    enrolled_credits: int = Field(default=0, ge=0)
    max_credits: int = Field(default=22, ge=0)


class CredentialVerifyRequest(BaseModel):
    student_id: str = Field(..., description="Student identifier, e.g. STU_001")
    password: str = Field(..., min_length=1, description="Plaintext password to verify")


class CredentialVerifyResponse(BaseModel):
    valid: bool
    student_id: str
    role: str = "STUDENT"
    name: str | None = None
    message: str | None = None


class FinancialHoldUpdateRequest(BaseModel):
    financial_hold: bool = Field(..., description="Set true to lock registration, false to release")


class EnrolledCreditsUpdateRequest(BaseModel):
    enrolled_credits: int = Field(..., ge=0, description="New enrolled credits tally")


class CompletedCoursesUpdateRequest(BaseModel):
    completed_courses: list[str] = Field(
        ..., description="List of course codes completed by student, e.g. ['CS101', 'CS102']"
    )


class StudentProfileDetailResponse(StudentDTO):
    email: str
    major: str
    year: int
