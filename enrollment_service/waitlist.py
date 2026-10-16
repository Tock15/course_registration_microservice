"""FIFO waitlist with time-limited seat offers."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.events import WaitlistSlotOfferedEvent
from common.schemas import EnrollmentStatus
from enrollment_service.config import OFFER_TTL_HOURS
from enrollment_service.models import Enrollment, utcnow
from enrollment_service.repository import apply_event, find_droppable
from enrollment_service.state_machine import LifecycleEvent

_WAITLISTED = EnrollmentStatus.WAITLISTED.value
_OFFERED = EnrollmentStatus.OFFERED.value


class NotEnrolledError(Exception):
    """The student has no droppable enrollment in the section."""


@dataclass(frozen=True)
class Offer:
    enrollment_id: int
    student_id: str
    section_id: int
    course_code: str
    expires_at: datetime

    def event(self) -> WaitlistSlotOfferedEvent:
        """Notification telling the student to claim before expires_at."""
        return WaitlistSlotOfferedEvent(
            offer_id=self.enrollment_id,
            student_id=self.student_id,
            section_id=self.section_id,
            course_code=self.course_code,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class DropOutcome:
    enrollment_id: int
    section_id: int
    course_code: str
    previous_status: EnrollmentStatus
    offered: Offer | None
    pending_release_key: str | None


@dataclass(frozen=True)
class ExpiryOutcome:
    enrollment_id: int
    section_id: int
    offered: Offer | None
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


async def student_queue(
    session: AsyncSession, student_id: str
) -> list[tuple[Enrollment, int]]:
    """The student's WAITLISTED and OFFERED rows with positions; an offer counts as 1."""
    result = await session.execute(
        select(Enrollment)
        .where(
            Enrollment.student_id == student_id,
            Enrollment.status.in_((_WAITLISTED, _OFFERED)),
        )
        .order_by(Enrollment.waitlisted_at, Enrollment.id)
    )
    queue: list[tuple[Enrollment, int]] = []
    for row in result.scalars().all():
        if row.status == _OFFERED:
            queue.append((row, 1))
        else:
            pos = await position_for(session, row.section_id, row.waitlisted_at, row.id)
            queue.append((row, pos))
    return queue


async def sections_with_waitlist(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(Enrollment.section_id)
        .where(Enrollment.status == _WAITLISTED)
        .distinct()
        .order_by(Enrollment.section_id)
    )
    return list(result.scalars().all())


async def offer_head(
    session: AsyncSession,
    section_id: int,
    seat_key: str,
    ttl_hours: float = OFFER_TTL_HOURS,
) -> Offer | None:
    """Hold seat_key for the queue head until they claim it or the offer expires."""
    candidate = await head(session, section_id)
    if candidate is None:
        return None

    expires_at = utcnow() + timedelta(hours=ttl_hours)
    offered = await apply_event(
        session,
        candidate.id,
        LifecycleEvent.PROMOTE,
        seat_key=seat_key,
        offer_expires_at=expires_at,
        reason="Seat offered from the waitlist.",
    )
    if offered is None:
        return None

    return Offer(
        enrollment_id=candidate.id,
        student_id=candidate.student_id,
        section_id=section_id,
        course_code=candidate.course_code,
        expires_at=expires_at,
    )


async def _give_up_seat(
    session: AsyncSession, row: Enrollment, event: LifecycleEvent, reason: str
) -> tuple[Offer | None, str | None]:
    """Move a seat holder out; the seat goes to the queue head, else back to Catalog."""
    seat_key = row.seat_key
    if not await has_waitlist(session, row.section_id):
        await apply_event(session, row.id, event, reason=f"{reason}.")
        return None, seat_key

    await apply_event(
        session,
        row.id,
        event,
        seat_key=None,
        reason=f"{reason}; seat passed to the waitlist.",
    )
    offered = await offer_head(session, row.section_id, seat_key)
    if offered is None:
        await session.execute(
            update(Enrollment).where(Enrollment.id == row.id).values(seat_key=seat_key)
        )
        return None, seat_key
    return offered, None


async def drop(session: AsyncSession, student_id: str, section_id: int) -> DropOutcome:
    """Drop a seat, decline an offer or leave the queue; freed seats go to the queue head."""
    row = await find_droppable(session, student_id, section_id)
    if row is None:
        raise NotEnrolledError(
            f"No active enrollment or waitlist entry in section {section_id}."
        )

    previous = EnrollmentStatus(row.status)
    offered: Offer | None = None
    pending_release_key: str | None = None

    if previous is EnrollmentStatus.WAITLISTED:
        await apply_event(session, row.id, LifecycleEvent.DROP, reason="Left the waitlist.")
    else:
        reason = (
            "Declined the seat offer"
            if previous is EnrollmentStatus.OFFERED
            else "Dropped by student"
        )
        offered, pending_release_key = await _give_up_seat(
            session, row, LifecycleEvent.DROP, reason
        )

    return DropOutcome(
        enrollment_id=row.id,
        section_id=section_id,
        course_code=row.course_code,
        previous_status=previous,
        offered=offered,
        pending_release_key=pending_release_key,
    )


async def expire_offer(session: AsyncSession, enrollment_id: int) -> ExpiryOutcome | None:
    """Expire an unclaimed offer past its deadline; None if it was claimed or is still live."""
    row = (
        await session.execute(
            select(Enrollment)
            .where(
                Enrollment.id == enrollment_id,
                Enrollment.status == _OFFERED,
                Enrollment.offer_expires_at <= utcnow(),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    offered, pending_release_key = await _give_up_seat(
        session, row, LifecycleEvent.EXPIRE, "Seat offer expired without a claim"
    )
    return ExpiryOutcome(
        enrollment_id=row.id,
        section_id=row.section_id,
        offered=offered,
        pending_release_key=pending_release_key,
    )
