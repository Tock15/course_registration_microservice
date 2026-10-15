"""
notification_service/main.py
Notification & Event Consumer Service (Port 8004).

Responsibilities:
  - Event Consumer & Subscriber (GoF Observer Pattern): event_consumer.py
  - Notification dispatch & audit store: models.py, database.py
  - Endpoints: POST /events, GET /notifications/{student_id}, POST /reset
"""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from notification_service.config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from notification_service.database import async_session_maker, get_db, init_db
from notification_service.event_consumer import (
    EventDispatcher,
    create_default_dispatcher,
)
from notification_service.models import NotificationLog
from notification_service.schemas import EventIngestResponse, NotificationDTO
from notification_service.seed import reset_notification_data


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize SQLite database and configure the Observer event dispatcher."""
    await init_db()
    app.state.dispatcher = create_default_dispatcher(async_session_maker)
    yield


app = FastAPI(
    title="Notification Service",
    description="Asynchronous subscriber service dispatching and storing registration alerts (Observer Pattern).",
    version="0.1.0",
    lifespan=lifespan,
)


def get_dispatcher(request: Request) -> EventDispatcher:
    """Dependency injection yielding the active EventDispatcher instance."""
    return request.app.state.dispatcher


# ---------------------------------------------------------
# Health & Service Information
# ---------------------------------------------------------


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, Any]:
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "notification_service",
        "status": "healthy",
        "port": 8004,
    }


@app.get("/", tags=["Info"])
async def root() -> dict[str, str]:
    return {
        "service": "Notification Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }


@app.post("/reset", tags=["Admin"])
async def reset_notifications(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Reset the notifications database to a clean, empty state."""
    if os.getenv("ALLOW_RESET", "false").lower() not in ("true", "1"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Database reset is disabled in production.",
        )
    details = await reset_notification_data(session=session)
    return {"ok": True, "details": details}


# ---------------------------------------------------------
# Asynchronous Event Ingestion (Observer Pattern)
# ---------------------------------------------------------


@app.post(
    "/events",
    response_model=EventIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Events"],
)
async def ingest_event(
    payload: dict[str, Any] = Body(
        ...,
        description="Domain event payload dispatched by Enrollment Service or other producers.",
        examples=[
            {
                "event_id": "test-uuid-123",
                "event_type": "ENROLLMENT_CONFIRMED",
                "student_id": "STU_001",
                "section_id": 101,
                "course_code": "CS301",
                "credits": 3,
            }
        ],
    ),
    dispatcher: EventDispatcher = Depends(get_dispatcher),
) -> EventIngestResponse:
    """Ingest an asynchronous domain event and notify all registered observers.

    Guarantees:
      - Fire-and-forget safety: Handlers execute without failing the HTTP response.
      - Decoupled transaction boundary: Returns HTTP 202 Accepted.
    """
    event_type = payload.get("event_type")
    if not event_type or not isinstance(event_type, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must include a non-empty 'event_type' string.",
        )

    handled_count = await dispatcher.dispatch(payload)
    return EventIngestResponse(
        accepted=True,
        event_id=payload.get("event_id"),
        event_type=event_type,
        handlers_notified=handled_count,
        detail=f"Dispatched {event_type} to {handled_count} observer(s).",
    )


# ---------------------------------------------------------
# Notification Inquiries & In-App Alerts
# ---------------------------------------------------------


@app.get(
    "/notifications/{student_id}",
    response_model=list[NotificationDTO],
    tags=["Notifications"],
)
async def get_student_notifications(
    student_id: str,
    limit: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="Maximum number of alerts to return.",
    ),
    session: AsyncSession = Depends(get_db),
) -> list[NotificationDTO]:
    """Retrieve chronological notification alerts for a student (newest first).

    Returns an empty list if the student has received no alerts (safe zero-state).
    """
    norm_id = student_id.strip().upper()
    stmt = (
        select(NotificationLog)
        .where(NotificationLog.recipient_id == norm_id)
        .order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return [NotificationDTO.model_validate(r) for r in records]
