"""FIFO waitlist with automatic promotion."""
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.schemas import EnrollmentStatus
from enrollment_service.models import Enrollment
from enrollment_service.repository import apply_event, find_droppable
from enrollment_service.state_machine import LifecycleEvent

_WAITLISTED = EnrollmentStatus.WAITLISTED.value


class NotEnrolledError(Exception):
    """The student has no droppable enrollment in the section."""


@dataclass(frozen=True)
class Promotion:
    enrollment_id: int
    student_id: str
    section_id: int
    course_code: str
    credits: int


@dataclass(frozen=True)
class DropOutcome:
    enrollment_id: int
    section_id: int
    course_code: str
    previous_status: EnrollmentStatus
    promoted: Promotion | None
    pending_release_key: str | None


async def has_waitlist(session: AsyncSession, section_id: int) -> bool:
    result = await session.execute(
        select(Enrollment.id)
        .where(Enrollment.section_id == section_id, Enrollment.status == _WAITLISTED)
        .limit(1)
    )
    return result.first() is not None


async def head(session: AsyncSession, section_id: int) -> Enrollment | None:
    """Oldest waitlisted row for the section (FIFO by arrival, then id)."""
    result = await session.execute(
        select(Enrollment)
        .where(Enrollment.section_id == section_id, Enrollment.status == _WAITLISTED)
        .order_by(Enrollment.waitlisted_at, Enrollment.id)
        .limit(1)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def position_for(
    session: AsyncSession,
    section_id: int,
    waitlisted_at: object,
    enrollment_id: int,
) -> int:
    """1-based queue position when section_id and waitlisted_at are already known."""
    ahead_or_self = await session.scalar(
        select(func.count(Enrollment.id)).where(
            Enrollment.section_id == section_id,
            Enrollment.status == _WAITLISTED,
            or_(
                Enrollment.waitlisted_at < waitlisted_at,
                and_(
                    Enrollment.waitlisted_at == waitlisted_at,
                    Enrollment.id <= enrollment_id,
                ),
            ),
        )
    )
    return int(ahead_or_self or 0)


async def position(session: AsyncSession, enrollment_id: int) -> int | None:
    """1-based queue position, or None if the row is not WAITLISTED."""
    row = (
        await session.execute(
            select(Enrollment.section_id, Enrollment.waitlisted_at).where(
                Enrollment.id == enrollment_id, Enrollment.status == _WAITLISTED
            )
        )
    ).one_or_none()
    if row is None:
        return None

    return await position_for(session, row.section_id, row.waitlisted_at, enrollment_id)


async def sections_with_waitlist(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(Enrollment.section_id)
        .where(Enrollment.status == _WAITLISTED)
        .distinct()
        .order_by(Enrollment.section_id)
    )
    return list(result.scalars().all())


async def promote_head(
    session: AsyncSession, section_id: int, seat_key: str
) -> Promotion | None:
    """Give seat_key to the queue head and enroll them."""
    candidate = await head(session, section_id)
    if candidate is None:
        return None

    promoted = await apply_event(
        session,
        candidate.id,
        LifecycleEvent.PROMOTE,
        seat_key=seat_key,
        reason="Promoted from waitlist.",
    )
    if promoted is None:
        return None

    return Promotion(
        enrollment_id=candidate.id,
        student_id=candidate.student_id,
        section_id=section_id,
        course_code=candidate.course_code,
        credits=candidate.credits,
    )


async def drop(session: AsyncSession, student_id: str, section_id: int) -> DropOutcome:
    """Drop an ENROLLED or WAITLISTED row; an ENROLLED seat goes to the queue head."""
    row = await find_droppable(session, student_id, section_id)
    if row is None:
        raise NotEnrolledError(
            f"No active enrollment or waitlist entry in section {section_id}."
        )

    previous = EnrollmentStatus(row.status)
    seat_key = row.seat_key
    promoted: Promotion | None = None
    pending_release_key: str | None = None

    if previous is EnrollmentStatus.WAITLISTED:
        await apply_event(session, row.id, LifecycleEvent.DROP, reason="Left the waitlist.")
    elif await has_waitlist(session, section_id):
        await apply_event(
            session,
            row.id,
            LifecycleEvent.DROP,
            seat_key=None,
            reason="Dropped by student; seat passed to the waitlist.",
        )
        promoted = await promote_head(session, section_id, seat_key)
        if promoted is None:
            await session.execute(
                update(Enrollment).where(Enrollment.id == row.id).values(seat_key=seat_key)
            )
            pending_release_key = seat_key
    else:
        await apply_event(session, row.id, LifecycleEvent.DROP, reason="Dropped by student.")
        pending_release_key = seat_key

    return DropOutcome(
        enrollment_id=row.id,
        section_id=section_id,
        course_code=row.course_code,
        previous_status=previous,
        promoted=promoted,
        pending_release_key=pending_release_key,
    )
