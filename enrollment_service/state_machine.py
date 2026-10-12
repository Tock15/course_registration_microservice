"""
enrollment_service/state_machine.py
State Pattern governing the enrollment lifecycle.

    STAGED ──SEAT_RESERVED──────▶ ENROLLED ──DROP──▶ DROPPED
       │                             ▲
       ├──SECTION_FULL──▶ WAITLISTED ┘ (PROMOTE)
       │                     └──DROP──▶ DROPPED
       └──RESERVATION_FAILED──▶ REJECTED

STAGED means "seat request in flight at Catalog". OFFERED from the shared
EnrollmentStatus enum is unused: waitlisted students are auto-enrolled.

The repository derives its conditional SQL (WHERE status IN ...) from this
module, so code and database agree on which transitions are legal.
"""
from abc import ABC
from enum import Enum
from functools import cache
from typing import ClassVar

from common.schemas import EnrollmentStatus


class InvalidTransition(Exception):
    """Raised when an event is not allowed from the current state."""

    def __init__(self, status: EnrollmentStatus, event: "LifecycleEvent") -> None:
        self.status = status
        self.event = event
        super().__init__(f"Cannot apply {event.value} to an enrollment in {status.value}.")


class LifecycleEvent(str, Enum):
    SEAT_RESERVED = "SEAT_RESERVED"
    SECTION_FULL = "SECTION_FULL"
    RESERVATION_FAILED = "RESERVATION_FAILED"
    PROMOTE = "PROMOTE"
    DROP = "DROP"


class EnrollmentState(ABC):
    """Base state: every event is invalid unless a concrete state allows it."""

    status: ClassVar[EnrollmentStatus]
    is_active: ClassVar[bool] = False

    def on_seat_reserved(self) -> "EnrollmentState":
        raise InvalidTransition(self.status, LifecycleEvent.SEAT_RESERVED)

    def on_section_full(self) -> "EnrollmentState":
        raise InvalidTransition(self.status, LifecycleEvent.SECTION_FULL)

    def on_reservation_failed(self) -> "EnrollmentState":
        raise InvalidTransition(self.status, LifecycleEvent.RESERVATION_FAILED)

    def on_promote(self) -> "EnrollmentState":
        raise InvalidTransition(self.status, LifecycleEvent.PROMOTE)

    def on_drop(self) -> "EnrollmentState":
        raise InvalidTransition(self.status, LifecycleEvent.DROP)


class StagedState(EnrollmentState):
    status = EnrollmentStatus.STAGED
    is_active = True

    def on_seat_reserved(self) -> EnrollmentState:
        return EnrolledState()

    def on_section_full(self) -> EnrollmentState:
        return WaitlistedState()

    def on_reservation_failed(self) -> EnrollmentState:
        return RejectedState()


class EnrolledState(EnrollmentState):
    status = EnrollmentStatus.ENROLLED
    is_active = True

    def on_drop(self) -> EnrollmentState:
        return DroppedState()


class WaitlistedState(EnrollmentState):
    status = EnrollmentStatus.WAITLISTED
    is_active = True

    def on_promote(self) -> EnrollmentState:
        return EnrolledState()

    def on_drop(self) -> EnrollmentState:
        return DroppedState()


class DroppedState(EnrollmentState):
    status = EnrollmentStatus.DROPPED


class RejectedState(EnrollmentState):
    status = EnrollmentStatus.REJECTED


_STATES: dict[EnrollmentStatus, EnrollmentState] = {
    state.status: state
    for state in (
        StagedState(),
        EnrolledState(),
        WaitlistedState(),
        DroppedState(),
        RejectedState(),
    )
}

_HANDLERS = {
    LifecycleEvent.SEAT_RESERVED: "on_seat_reserved",
    LifecycleEvent.SECTION_FULL: "on_section_full",
    LifecycleEvent.RESERVATION_FAILED: "on_reservation_failed",
    LifecycleEvent.PROMOTE: "on_promote",
    LifecycleEvent.DROP: "on_drop",
}


def _fire(state: EnrollmentState, event: LifecycleEvent) -> EnrollmentState:
    return getattr(state, _HANDLERS[event])()

ACTIVE_STATUSES: tuple[str, ...] = tuple(
    s.status.value for s in _STATES.values() if s.is_active
)
TERMINAL_STATUSES: tuple[str, ...] = tuple(
    s.status.value for s in _STATES.values() if not s.is_active
)


def state_of(status: str | EnrollmentStatus) -> EnrollmentState:
    """Return the state object for a stored status value."""
    try:
        return _STATES[EnrollmentStatus(status)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unsupported enrollment status: {status!r}") from exc


def next_status(
    status: str | EnrollmentStatus, event: LifecycleEvent
) -> EnrollmentStatus:
    """Apply an event to a status; raises InvalidTransition if not allowed."""
    return _fire(state_of(status), event).status


@cache
def sources_for(event: LifecycleEvent) -> tuple[str, ...]:
    """Statuses from which the event is legal (used in conditional SQL)."""
    sources: list[str] = []
    for state in _STATES.values():
        try:
            _fire(state, event)
        except InvalidTransition:
            continue
        sources.append(state.status.value)
    return tuple(sources)
