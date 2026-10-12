"""Enrollment-specific response schemas."""
from datetime import datetime

from pydantic import BaseModel, Field

from common.schemas import EnrollmentResponse, EnrollmentStatus


class EnrollmentResult(EnrollmentResponse):
    """EnrollmentResponse plus waitlist position and the failing rule, if any."""

    waitlist_position: int | None = Field(
        default=None, description="1-based FIFO position when WAITLISTED"
    )
    rule_name: str | None = Field(
        default=None, description="Validation rule that rejected the request"
    )


class EnrollmentRecord(BaseModel):
    enrollment_id: int
    section_id: int
    course_code: str
    credits: int
    status: EnrollmentStatus
    schedule_days: list[str] = Field(default_factory=list)
    start_time: str
    end_time: str
    waitlist_position: int | None = None
    reason: str | None = None
    created_at: datetime
    updated_at: datetime


class MyEnrollmentsResponse(BaseModel):
    student_id: str
    total_credits: int = Field(description="Credits of ENROLLED sections only")
    enrollments: list[EnrollmentRecord] = Field(default_factory=list)
