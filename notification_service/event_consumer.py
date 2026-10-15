"""
notification_service/event_consumer.py
Asynchronous subscriber and event consumer implementing the Gang of Four (GoF) Observer Pattern.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.events import (
    CourseDroppedEvent,
    EnrollmentConfirmedEvent,
    WaitlistJoinedEvent,
    WaitlistSlotOfferedEvent,
)
from notification_service.models import NotificationLog

logger = logging.getLogger(__name__)


class EventObserver(ABC):
    """Abstract Observer interface defining the notification delivery contract."""

    name: str

    @abstractmethod
    async def handle(
        self,
        event_data: dict[str, Any],
        session_maker: async_sessionmaker[AsyncSession],
    ) -> bool:
        """Process domain event and return True if alert was generated and persisted."""


class EnrollmentConfirmedObserver(EventObserver):
    """Concrete Observer handling course registration confirmation alerts."""

    name = "EnrollmentConfirmedObserver"

    async def handle(
        self,
        event_data: dict[str, Any],
        session_maker: async_sessionmaker[AsyncSession],
    ) -> bool:
        try:
            event = EnrollmentConfirmedEvent.model_validate(event_data)
        except ValidationError as exc:
            logger.warning("Dropped invalid ENROLLMENT_CONFIRMED event: %s", exc)
            return False

        subject = f"Registration Confirmed: {event.course_code}"
        message = (
            f"You have been successfully enrolled in {event.course_code} "
            f"(Section {event.section_id}) for {event.credits} credits."
        )

        log = NotificationLog(
            event_id=event.event_id,
            recipient_id=event.student_id.strip().upper(),
            event_type="ENROLLMENT_CONFIRMED",
            subject=subject,
            message=message,
            channel="IN_APP",
            status="DELIVERED",
        )
        async with session_maker() as session, session.begin():
            session.add(log)

        logger.info(
            "Delivered ENROLLMENT_CONFIRMED alert to student %s for %s Sec %s",
            event.student_id,
            event.course_code,
            event.section_id,
        )
        return True


class WaitlistJoinedObserver(EventObserver):
    """Concrete Observer handling waitlist placement and queue position alerts."""

    name = "WaitlistJoinedObserver"

    async def handle(
        self,
        event_data: dict[str, Any],
        session_maker: async_sessionmaker[AsyncSession],
    ) -> bool:
        try:
            event = WaitlistJoinedEvent.model_validate(event_data)
        except ValidationError as exc:
            logger.warning("Dropped invalid WAITLIST_JOINED event: %s", exc)
            return False

        subject = f"Waitlist Confirmation: {event.course_code}"
        message = (
            f"Section {event.section_id} of {event.course_code} is currently full. "
            f"You have been added to the waitlist at queue position #{event.position}."
        )

        log = NotificationLog(
            event_id=event.event_id,
            recipient_id=event.student_id.strip().upper(),
            event_type="WAITLIST_JOINED",
            subject=subject,
            message=message,
            channel="IN_APP",
            status="DELIVERED",
        )
        async with session_maker() as session, session.begin():
            session.add(log)

        logger.info(
            "Delivered WAITLIST_JOINED alert to student %s for %s Sec %s (Rank #%s)",
            event.student_id,
            event.course_code,
            event.section_id,
            event.position,
        )
        return True


class CourseDroppedObserver(EventObserver):
    """Concrete Observer handling course relinquishment notices."""

    name = "CourseDroppedObserver"

    async def handle(
        self,
        event_data: dict[str, Any],
        session_maker: async_sessionmaker[AsyncSession],
    ) -> bool:
        try:
            event = CourseDroppedEvent.model_validate(event_data)
        except ValidationError as exc:
            logger.warning("Dropped invalid COURSE_DROPPED event: %s", exc)
            return False

        subject = f"Course Dropped: {event.course_code}"
        message = (
            f"You have dropped section {event.section_id} of {event.course_code}. "
            "Your seat reservation has been released."
        )

        log = NotificationLog(
            event_id=event.event_id,
            recipient_id=event.student_id.strip().upper(),
            event_type="COURSE_DROPPED",
            subject=subject,
            message=message,
            channel="IN_APP",
            status="DELIVERED",
        )
        async with session_maker() as session, session.begin():
            session.add(log)

        logger.info(
            "Delivered COURSE_DROPPED alert to student %s for %s Sec %s",
            event.student_id,
            event.course_code,
            event.section_id,
        )
        return True


class WaitlistSlotOfferedObserver(EventObserver):
    """Concrete Observer handling time-limited waitlist seat claim offers."""

    name = "WaitlistSlotOfferedObserver"

    async def handle(
        self,
        event_data: dict[str, Any],
        session_maker: async_sessionmaker[AsyncSession],
    ) -> bool:
        try:
            event = WaitlistSlotOfferedEvent.model_validate(event_data)
        except ValidationError as exc:
            logger.warning("Dropped invalid WAITLIST_SLOT_OFFERED event: %s", exc)
            return False

        subject = f"Seat Offer Available: {event.course_code}"
        message = (
            f"A seat has opened up for {event.course_code} (Section {event.section_id})! "
            f"Please claim your seat before {event.expires_at}."
        )

        log = NotificationLog(
            event_id=event.event_id,
            recipient_id=event.student_id.strip().upper(),
            event_type="WAITLIST_SLOT_OFFERED",
            subject=subject,
            message=message,
            channel="IN_APP",
            status="DELIVERED",
        )
        async with session_maker() as session, session.begin():
            session.add(log)

        logger.info(
            "Delivered WAITLIST_SLOT_OFFERED alert to student %s for %s Sec %s",
            event.student_id,
            event.course_code,
            event.section_id,
        )
        return True


class EventDispatcher:
    """Subject / Registry in the Observer Pattern.

    Maintains subscriber registrations and dispatches incoming events asynchronously
    to all registered concrete observers matching the event topic.
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker
        self._observers: dict[str, list[EventObserver]] = {}

    def register(self, event_type: str, observer: EventObserver) -> None:
        """Register an observer for a specific event topic."""
        self._observers.setdefault(event_type, []).append(observer)
        logger.debug("Registered observer %s for topic %s", observer.name, event_type)

    def has_topic(self, event_type: str) -> bool:
        """Return True if at least one observer is registered for this topic."""
        return bool(self._observers.get(event_type))

    async def dispatch(self, event_data: dict[str, Any]) -> int:
        """Dispatch an event payload to all matching observers. Never raises."""
        event_type = event_data.get("event_type", "UNKNOWN")
        observers = self._observers.get(event_type, [])

        if not observers:
            logger.warning("No registered observers for event type: %s", event_type)
            return 0

        handled_count = 0
        for observer in observers:
            try:
                success = await observer.handle(event_data, self._session_maker)
                if success:
                    handled_count += 1
            except Exception:
                logger.exception(
                    "Observer %s failed to handle event %s",
                    observer.name,
                    event_data.get("event_id"),
                )

        return handled_count


def create_default_dispatcher(
    session_maker: async_sessionmaker[AsyncSession],
) -> EventDispatcher:
    """Instantiate and configure the default Observer registry."""
    dispatcher = EventDispatcher(session_maker)
    dispatcher.register("ENROLLMENT_CONFIRMED", EnrollmentConfirmedObserver())
    dispatcher.register("WAITLIST_JOINED", WaitlistJoinedObserver())
    dispatcher.register("COURSE_DROPPED", CourseDroppedObserver())
    dispatcher.register("WAITLIST_SLOT_OFFERED", WaitlistSlotOfferedObserver())
    return dispatcher
