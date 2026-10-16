"""Background reconciler: stale reservations, expired offers, pending releases, waitlist pump."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    expired: int = 0
    released: int = 0
    offered: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.stale_rejected or self.expired or self.released or self.offered)


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
            expired=await self.expire_offers(),
            released=await self.release_pending(),
            offered=await self.pump_waitlists(),
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

    async def expire_offers(self) -> int:
        async with self._sessions() as session, session.begin():
            expired_ids = await repository.expired_offer_ids(session, utcnow())

        expired = 0
        for enrollment_id in expired_ids:
            async with self._sessions() as session, session.begin():
                outcome = await waitlist.expire_offer(session, enrollment_id)
            if outcome is None:
                continue
            expired += 1
            if outcome.offered:
                self._publisher.publish(outcome.offered.event())
        return expired

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

        offered = 0
        for section_id in section_ids:
            offered += await self._pump_section(section_id)
        return offered

    async def _pump_section(self, section_id: int) -> int:
        offered = 0
        while True:
            async with self._sessions() as session, session.begin():
                if not await waitlist.has_waitlist(session, section_id):
                    return offered

            seat_key = str(uuid4())
            try:
                if not await self._catalog.reserve_seat(section_id, seat_key):
                    return offered
            except (UpstreamUnavailable, UpstreamNotFound) as exc:
                logger.warning("Waitlist pump skipped section %s: %s", section_id, exc)
                return offered

            async with self._sessions() as session, session.begin():
                offer = await waitlist.offer_head(session, section_id, seat_key)

            if offer is None:
                try:
                    await self._catalog.release_seat(section_id, seat_key)
                except UpstreamUnavailable as exc:
                    logger.error(
                        "Leaked seat %s in section %s: %s", seat_key, section_id, exc
                    )
                return offered

            self._publisher.publish(offer.event())
            offered += 1
