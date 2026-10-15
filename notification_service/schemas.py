"""
notification_service/schemas.py
Pydantic schemas and DTOs for Notification Service requests and responses.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NotificationDTO(BaseModel):
    """Public representation of an in-app student alert."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    recipient_id: str
    event_type: str
    subject: str
    message: str
    channel: str = "IN_APP"
    status: str = "DELIVERED"
    created_at: datetime


class EventIngestResponse(BaseModel):
    """Response returned upon event ingestion."""

    accepted: bool = True
    event_id: str | None = None
    event_type: str
    handlers_notified: int = Field(
        ..., description="Number of concrete observers that handled this event."
    )
    detail: str = "Event ingested and dispatched successfully."
