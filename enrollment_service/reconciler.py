"""Background reconciler: stale reservations, pending releases, waitlist pump."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.events import EnrollmentConfirmedEvent
from enrollment_service import repository, waitlist
from enrollment_service.clients import (
    CatalogClient,
    UpstreamNotFound,
    UpstreamUnavailable,
)
from enrollment_service.config import STAGED_STALE_SECONDS
from enrollment_service.events import EventPublisher
from enrollment_service.models import utcnow
from enrollment_service.seat_allocator import SeatAllocator
from enrollment_service.state_machine import LifecycleEvent

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    stale_rejected: int = 0
    released: int = 0
    promoted: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.stale_rejected or self.released or self.promoted)


class Reconciler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: CatalogClient,
        publisher: EventPublisher,
        *,
        stale_after_seconds: float = STAGED_STALE_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
        self._publisher = publisher
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._seats = SeatAllocator(sessions, catalog)

    async def run_forever(self, interval_seconds: float) -> None:
        while True:
            try:
                report = await self.run_once()
                if report.changed:
                    logger.info("Reconciler pass: %s", report)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciler pass failed")
            await asyncio.sleep(interval_seconds)

    async def run_once(self) -> ReconcileReport:
        return ReconcileReport(
            stale_rejected=await self.reject_stale_staged(),
            released=await self.release_pending(),
            promoted=await self.pump_waitlists(),
        )

    async def reject_stale_staged(self) -> int:
        cutoff = utcnow() - self._stale_after
        rejected = 0
        async with self._sessions() as session, session.begin():
            for enrollment_id in await repository.stale_staged_ids(session, cutoff):
                status = await repository.apply_event(
                    session,
                    enrollment_id,
                    LifecycleEvent.RESERVATION_FAILED,
                    reason="Seat reservation abandoned (no response).",
                )
                rejected += status is not None
        return rejected

    async def release_pending(self) -> int:
        async with self._sessions() as session, session.begin():
            pending = await repository.pending_releases(session)

        released = 0
        for enrollment_id, section_id, seat_key in pending:
            released += await self._seats.release(enrollment_id, section_id, seat_key)
        return released

    async def pump_waitlists(self) -> int:
        async with self._sessions() as session, session.begin():
            section_ids = await waitlist.sections_with_waitlist(session)

        promoted = 0
        for section_id in section_ids:
            promoted += await self._pump_section(section_id)
        return promoted

    async def _pump_section(self, section_id: int) -> int:
        promoted = 0
        while True:
            async with self._sessions() as session, session.begin():
                if not await waitlist.has_waitlist(session, section_id):
                    return promoted

            seat_key = str(uuid4())
            try:
                if not await self._catalog.reserve_seat(section_id, seat_key):
                    return promoted
            except (UpstreamUnavailable, UpstreamNotFound) as exc:
                logger.warning("Waitlist pump skipped section %s: %s", section_id, exc)
                return promoted

            async with self._sessions() as session, session.begin():
                promotion = await waitlist.promote_head(session, section_id, seat_key)

            if promotion is None:
                try:
                    await self._catalog.release_seat(section_id, seat_key)
                except UpstreamUnavailable as exc:
                    logger.error(
                        "Leaked seat %s in section %s: %s", seat_key, section_id, exc
                    )
                return promoted

            self._publisher.publish(
                EnrollmentConfirmedEvent(
                    student_id=promotion.student_id,
                    section_id=section_id,
                    course_code=promotion.course_code,
                    credits=promotion.credits,
                )
            )
            promoted += 1
