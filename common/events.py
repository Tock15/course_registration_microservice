"""
common/events.py
Domain event schemas used for asynchronous event publishing (Observer Pattern).
"""
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )


class EnrollmentConfirmedEvent(BaseEvent):
    event_type: str = "ENROLLMENT_CONFIRMED"
    student_id: str
    section_id: int
    course_code: str
    credits: int = 3


class WaitlistSlotOfferedEvent(BaseEvent):
    event_type: str = "WAITLIST_SLOT_OFFERED"
    offer_id: int
    student_id: str
    section_id: int
    course_code: str
    expires_at: datetime


class CourseDroppedEvent(BaseEvent):
    event_type: str = "COURSE_DROPPED"
    student_id: str
    section_id: int
    course_code: str


class NotificationRecord(BaseModel):
    """Stored representation of notifications sent to students."""
    id: int | None = None
    recipient_id: str
    event_type: str
    subject: str
    message: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
