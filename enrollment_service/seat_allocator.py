"""Seat allocation against Catalog using idempotent seat tokens."""
import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.schemas import EnrollmentStatus, SectionDTO
from enrollment_service import repository, waitlist
from enrollment_service.clients import (
    CatalogClient,
    UpstreamNotFound,
    UpstreamUnavailable,
)
from enrollment_service.state_machine import LifecycleEvent

logger = logging.getLogger(__name__)


class DuplicateEnrollmentError(Exception):
    """The student already has an active row for this section."""


@dataclass(frozen=True)
class Allocation:
    enrollment_id: int
    status: EnrollmentStatus
    waitlist_position: int | None = None


class SeatAllocator:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], catalog: CatalogClient
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog

    async def allocate(
        self, student_id: str, section: SectionDTO, credits: int
    ) -> Allocation:
        seat_key = str(uuid4())

        try:
            async with self._sessions() as session, session.begin():
                if await waitlist.has_waitlist(session, section.id):
                    row = await repository.add_enrollment(
                        session,
                        student_id=student_id,
                        section=section,
                        credits=credits,
                        status=EnrollmentStatus.WAITLISTED,
                        reason="Joined behind the existing waitlist.",
                    )
                    enrollment_id, arrived_at, queued = row.id, row.created_at, True
                else:
                    row = await repository.add_enrollment(
                        session,
                        student_id=student_id,
                        section=section,
                        credits=credits,
                        status=EnrollmentStatus.STAGED,
                        seat_key=seat_key,
                    )
                    enrollment_id, arrived_at, queued = row.id, row.created_at, False
        except IntegrityError as exc:
            raise DuplicateEnrollmentError(
                f"Student '{student_id}' already has an active request "
                f"for section {section.id}."
            ) from exc

        if queued:
            return Allocation(
                enrollment_id, EnrollmentStatus.WAITLISTED, await self._position(enrollment_id)
            )

        try:
            reserved = await self._catalog.reserve_seat(section.id, seat_key)
        except (UpstreamUnavailable, UpstreamNotFound) as exc:
            await self._finish(
                enrollment_id,
                LifecycleEvent.RESERVATION_FAILED,
                reason=f"Seat reservation failed: {exc}",
            )
            await self.release(enrollment_id, section.id, seat_key)
            raise

        if reserved:
            status = await self._finish(enrollment_id, LifecycleEvent.SEAT_RESERVED)
        else:
            status = await self._finish(
                enrollment_id,
                LifecycleEvent.SECTION_FULL,
                reason="Section full.",
                seat_key=None,
                waitlisted_at=arrived_at,
            )

        if status is None:
            raise UpstreamUnavailable(
                self._catalog.service_name, "Seat reservation timed out; please retry."
            )
        if status is EnrollmentStatus.WAITLISTED:
            return Allocation(enrollment_id, status, await self._position(enrollment_id))
        return Allocation(enrollment_id, status)

    async def release(self, enrollment_id: int, section_id: int, seat_key: str) -> bool:
        """Return a seat to Catalog; False if unreachable (reconciler retries)."""
        try:
            await self._catalog.release_seat(section_id, seat_key)
        except UpstreamUnavailable as exc:
            logger.warning(
                "Seat release pending for enrollment %s (section %s): %s",
                enrollment_id,
                section_id,
                exc,
            )
            return False

        async with self._sessions() as session, session.begin():
            await repository.clear_seat_key(session, enrollment_id, seat_key)
        return True

    async def _finish(
        self, enrollment_id: int, event: LifecycleEvent, *, reason: str | None = None, **values
    ) -> EnrollmentStatus | None:
        async with self._sessions() as session, session.begin():
            return await repository.apply_event(
                session, enrollment_id, event, reason=reason, **values
            )

    async def _position(self, enrollment_id: int) -> int | None:
        async with self._sessions() as session, session.begin():
            return await waitlist.position(session, enrollment_id)
