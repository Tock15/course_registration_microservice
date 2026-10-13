"""Seat allocation against Catalog using idempotent seat tokens."""
import logging
import time
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
from enrollment_service.database import run_with_retry
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
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: CatalogClient,
        ttl_seconds: float = 15.0,
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
        self._ttl_seconds = ttl_seconds
        # section_id -> monotonic expiry timestamp
        self._full_sections_ttl: dict[int, float] = {}

    def _is_section_full(self, section_id: int) -> bool:
        """Self-healing check: returns True only if marked full within the TTL window."""
        expiry = self._full_sections_ttl.get(section_id)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            self._full_sections_ttl.pop(section_id, None)
            return False
        return True

    def mark_section_full(self, section_id: int) -> None:
        """Mark section saturated for the TTL duration to absorb burst traffic."""
        self._full_sections_ttl[section_id] = time.monotonic() + self._ttl_seconds

    def mark_section_available(self, section_id: int) -> None:
        """Clear the full flag when a seat is freed and no waitlist remains."""
        self._full_sections_ttl.pop(section_id, None)

    def clear_cache(self) -> None:
        """Reset the in-memory saturation cache."""
        self._full_sections_ttl.clear()

    async def _enqueue_waitlist_direct(
        self, student_id: str, section: SectionDTO, credits: int
    ) -> Allocation:
        """Fast-path: direct waitlist append and position query in 1 local transaction."""
        async def _direct_tx():
            async with self._sessions() as session, session.begin():
                row = await repository.add_enrollment(
                    session,
                    student_id=student_id,
                    section=section,
                    credits=credits,
                    status=EnrollmentStatus.WAITLISTED,
                    reason="Joined behind the existing waitlist.",
                )
                pos = await waitlist.position(session, row.id)
                return row.id, pos

        try:
            enrollment_id, pos = await run_with_retry(_direct_tx)
        except IntegrityError as exc:
            raise DuplicateEnrollmentError(
                f"Student '{student_id}' already has an active request "
                f"for section {section.id}."
            ) from exc

        return Allocation(enrollment_id, EnrollmentStatus.WAITLISTED, pos)

    async def allocate(
        self, student_id: str, section: SectionDTO, credits: int
    ) -> Allocation:
        # Fast-path: section is already known to be full, bypass Catalog and STAGED state
        if self._is_section_full(section.id):
            return await self._enqueue_waitlist_direct(student_id, section, credits)

        seat_key = str(uuid4())

        async def _insert_staged():
            async with self._sessions() as session, session.begin():
                if await waitlist.has_waitlist(session, section.id):
                    self.mark_section_full(section.id)
                    row = await repository.add_enrollment(
                        session,
                        student_id=student_id,
                        section=section,
                        credits=credits,
                        status=EnrollmentStatus.WAITLISTED,
                        reason="Joined behind the existing waitlist.",
                    )
                    pos = await waitlist.position(session, row.id)
                    return row.id, row.created_at, pos, True
                else:
                    row = await repository.add_enrollment(
                        session,
                        student_id=student_id,
                        section=section,
                        credits=credits,
                        status=EnrollmentStatus.STAGED,
                        seat_key=seat_key,
                    )
                    return row.id, row.created_at, None, False

        try:
            enrollment_id, arrived_at, pos, queued = await run_with_retry(_insert_staged)
        except IntegrityError as exc:
            raise DuplicateEnrollmentError(
                f"Student '{student_id}' already has an active request "
                f"for section {section.id}."
            ) from exc

        if queued:
            return Allocation(enrollment_id, EnrollmentStatus.WAITLISTED, pos)

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
            status, pos = await self._finish(enrollment_id, LifecycleEvent.SEAT_RESERVED)
        else:
            self.mark_section_full(section.id)
            status, pos = await self._finish(
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
            return Allocation(enrollment_id, status, pos)
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
    ) -> tuple[EnrollmentStatus | None, int | None]:
        async def _do_finish():
            async with self._sessions() as session, session.begin():
                st = await repository.apply_event(
                    session, enrollment_id, event, reason=reason, **values
                )
                pos = None
                if st is EnrollmentStatus.WAITLISTED:
                    pos = await waitlist.position(session, enrollment_id)
                return st, pos

        return await run_with_retry(_do_finish)

    async def _position(self, enrollment_id: int) -> int | None:
        async def _do_position():
            async with self._sessions() as session, session.begin():
                return await waitlist.position(session, enrollment_id)
        return await run_with_retry(_do_position)
