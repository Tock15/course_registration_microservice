"""Enrollment-specific request and response schemas."""
from datetime import datetime
from typing import Literal

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
    offer_expires_at: datetime | None = Field(
        default=None, description="Claim deadline (UTC) while OFFERED"
    )
    reason: str | None = None
    created_at: datetime
    updated_at: datetime


class MyEnrollmentsResponse(BaseModel):
    student_id: str
    total_credits: int = Field(description="Credits of ENROLLED sections only")
    enrollments: list[EnrollmentRecord] = Field(default_factory=list)


class WaitlistEntry(BaseModel):
    """One of the caller's waitlist places or pending seat offers."""

    offer_id: int = Field(description="Enrollment id; pass it to POST /waitlist/claim")
    section_id: int
    course_code: str
    position: int = Field(description="1-based FIFO position; 1 while OFFERED")
    status: Literal[EnrollmentStatus.WAITLISTED, EnrollmentStatus.OFFERED]
    expires_at: datetime | None = Field(
        default=None, description="Claim deadline (UTC) once a seat is offered"
    )


class ClaimRequest(BaseModel):
    offer_id: int


class ClaimResult(BaseModel):
    """Outcome of a claim; an unclaimable offer is a normal result, not an error."""

    status: Literal["CLAIMED", "EXPIRED_OR_ALREADY_CLAIMED"]
    message: str
    enrollment_id: int | None = None
    section_id: int | None = None
    course_code: str | None = None
