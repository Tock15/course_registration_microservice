"""
notification_service/event_consumer.py
Asynchronous subscriber and event consumer implementing the Gang of Four (GoF) Observer Pattern.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        student_id = event_data.get("student_id")
        if not student_id:
            return False

        course_code = event_data.get("course_code", "Course")
        section_id = event_data.get("section_id", 0)
        credits_count = event_data.get("credits", 3)
        event_id = event_data.get("event_id", "")

        subject = f"Registration Confirmed: {course_code}"
        message = (
            f"You have been successfully enrolled in {course_code} "
            f"(Section {section_id}) for {credits_count} credits."
        )

        log = NotificationLog(
            event_id=event_id,
            recipient_id=student_id.strip().upper(),
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
            student_id,
            course_code,
            section_id,
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
        student_id = event_data.get("student_id")
        if not student_id:
            return False

        course_code = event_data.get("course_code", "Course")
        section_id = event_data.get("section_id", 0)
        position = event_data.get("position", 1)
        event_id = event_data.get("event_id", "")

        subject = f"Waitlist Confirmation: {course_code}"
        message = (
            f"Section {section_id} of {course_code} is currently full. "
            f"You have been added to the waitlist at queue position #{position}."
        )

        log = NotificationLog(
            event_id=event_id,
            recipient_id=student_id.strip().upper(),
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
            student_id,
            course_code,
            section_id,
            position,
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
        student_id = event_data.get("student_id")
        if not student_id:
            return False

        course_code = event_data.get("course_code", "Course")
        section_id = event_data.get("section_id", 0)
        event_id = event_data.get("event_id", "")

        subject = f"Course Dropped: {course_code}"
        message = (
            f"You have dropped section {section_id} of {course_code}. "
            "Your seat reservation has been released."
        )

        log = NotificationLog(
            event_id=event_id,
            recipient_id=student_id.strip().upper(),
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
            student_id,
            course_code,
            section_id,
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
        student_id = event_data.get("student_id")
        if not student_id:
            return False

        course_code = event_data.get("course_code", "Course")
        section_id = event_data.get("section_id", 0)
        expires_at = event_data.get("expires_at", "within 24 hours")
        event_id = event_data.get("event_id", "")

        subject = f"Seat Offer Available: {course_code}"
        message = (
            f"A seat has opened up for {course_code} (Section {section_id})! "
            f"Please claim your seat before {expires_at}."
        )

        log = NotificationLog(
            event_id=event_id,
            recipient_id=student_id.strip().upper(),
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
            student_id,
            course_code,
            section_id,
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
