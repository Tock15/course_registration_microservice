"""Data-access helpers; callers own the transaction."""
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.schemas import EnrollmentStatus, SectionDTO
from enrollment_service.models import AuditLog, Enrollment, utcnow
from enrollment_service.state_machine import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    InvalidTransition,
    LifecycleEvent,
    next_status,
    sources_for,
)


def record_audit(
    session: AsyncSession,
    *,
    student_id: str,
    section_id: int,
    action: str,
    enrollment_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: str | None = None,
) -> None:
    """Append an immutable audit entry (NFR: auditability & traceability)."""
    session.add(
        AuditLog(
            enrollment_id=enrollment_id,
            student_id=student_id,
            section_id=section_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            detail=detail,
            created_at=utcnow(),
        )
    )


async def add_enrollment(
    session: AsyncSession,
    *,
    student_id: str,
    section: SectionDTO,
    credits: int,
    status: EnrollmentStatus,
    seat_key: str | None = None,
    reason: str | None = None,
) -> Enrollment:
    """Insert a new enrollment row; IntegrityError if already active in section."""
    now = utcnow()
    row = Enrollment(
        student_id=student_id,
        section_id=section.id,
        course_code=section.course_code,
        credits=credits,
        start_time=section.start_time,
        end_time=section.end_time,
        status=status.value,
        seat_key=seat_key,
        waitlisted_at=now if status is EnrollmentStatus.WAITLISTED else None,
        reason=reason,
        created_at=now,
        updated_at=now,
    )
    row.schedule_days = section.schedule_days
    session.add(row)
    await session.flush()

    record_audit(
        session,
        enrollment_id=row.id,
        student_id=student_id,
        section_id=section.id,
        action="CREATED",
        to_status=status.value,
        detail=reason,
    )
    return row


async def apply_event(
    session: AsyncSession,
    enrollment_id: int,
    event: LifecycleEvent,
    *,
    reason: str | None = None,
    **values,
) -> EnrollmentStatus | None:
    """Apply a lifecycle event via conditional UPDATE; None if not allowed."""
    found = (
        await session.execute(
            select(Enrollment.status, Enrollment.student_id, Enrollment.section_id)
            .where(Enrollment.id == enrollment_id)
        )
    ).one_or_none()
    if found is None:
        return None

    current, student_id, section_id = found
    try:
        target = next_status(current, event)
    except InvalidTransition:
        return None

    if reason is not None:
        values["reason"] = reason
    result = await session.execute(
        update(Enrollment)
        .where(Enrollment.id == enrollment_id, Enrollment.status == current)
        .values(status=target.value, updated_at=utcnow(), **values)
    )
    if result.rowcount != 1:
        return None

    record_audit(
        session,
        enrollment_id=enrollment_id,
        student_id=student_id,
        section_id=section_id,
        action=event.value,
        from_status=current,
        to_status=target.value,
        detail=reason,
    )
    return target


async def active_enrollments(session: AsyncSession, student_id: str) -> list[Enrollment]:
    result = await session.execute(
        select(Enrollment).where(
            Enrollment.student_id == student_id,
            Enrollment.status.in_(ACTIVE_STATUSES),
        )
    )
    return list(result.scalars().all())


async def student_enrollments(session: AsyncSession, student_id: str) -> list[Enrollment]:
    result = await session.execute(
        select(Enrollment)
        .where(Enrollment.student_id == student_id)
        .order_by(Enrollment.created_at.desc(), Enrollment.id.desc())
    )
    return list(result.scalars().all())


async def find_droppable(
    session: AsyncSession, student_id: str, section_id: int
) -> Enrollment | None:
    result = await session.execute(
        select(Enrollment).where(
            Enrollment.student_id == student_id,
            Enrollment.section_id == section_id,
            Enrollment.status.in_(sources_for(LifecycleEvent.DROP)),
        )
    )
    return result.scalar_one_or_none()


async def stale_staged_ids(session: AsyncSession, older_than: datetime) -> list[int]:
    result = await session.execute(
        select(Enrollment.id).where(
            Enrollment.status == EnrollmentStatus.STAGED.value,
            Enrollment.created_at < older_than,
        )
    )
    return list(result.scalars().all())


async def pending_releases(session: AsyncSession) -> list[tuple[int, int, str]]:
    """Terminal rows still holding a seat token: (enrollment_id, section_id, seat_key)."""
    result = await session.execute(
        select(Enrollment.id, Enrollment.section_id, Enrollment.seat_key).where(
            Enrollment.status.in_(TERMINAL_STATUSES),
            Enrollment.seat_key.is_not(None),
        )
    )
    return [(row.id, row.section_id, row.seat_key) for row in result.all()]


async def clear_seat_key(session: AsyncSession, enrollment_id: int, seat_key: str) -> bool:
    """Forget a released seat token on a terminal row."""
    found = (
        await session.execute(
            select(Enrollment.student_id, Enrollment.section_id).where(
                Enrollment.id == enrollment_id,
                Enrollment.seat_key == seat_key,
                Enrollment.status.in_(TERMINAL_STATUSES),
            )
        )
    ).one_or_none()
    if found is None:
        return False

    await session.execute(
        update(Enrollment)
        .where(Enrollment.id == enrollment_id, Enrollment.seat_key == seat_key)
        .values(seat_key=None, updated_at=utcnow())
    )
    record_audit(
        session,
        enrollment_id=enrollment_id,
        student_id=found.student_id,
        section_id=found.section_id,
        action="SEAT_RELEASED",
        detail="Seat returned to Catalog.",
    )
    return True
