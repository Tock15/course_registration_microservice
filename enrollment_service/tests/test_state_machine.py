"""State machine tests."""
import pytest

from common.schemas import EnrollmentStatus as S
from enrollment_service.state_machine import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    InvalidTransition,
    LifecycleEvent,
    next_status,
    sources_for,
    state_of,
)

E = LifecycleEvent


@pytest.mark.parametrize(
    ("status", "event", "expected"),
    [
        (S.STAGED, E.SEAT_RESERVED, S.ENROLLED),
        (S.STAGED, E.SECTION_FULL, S.WAITLISTED),
        (S.STAGED, E.RESERVATION_FAILED, S.REJECTED),
        (S.WAITLISTED, E.PROMOTE, S.ENROLLED),
        (S.WAITLISTED, E.DROP, S.DROPPED),
        (S.ENROLLED, E.DROP, S.DROPPED),
    ],
)
def test_allowed_transitions(status, event, expected):
    assert next_status(status, event) is expected
    assert next_status(status.value, event) is expected


@pytest.mark.parametrize(
    ("status", "event"),
    [
        (S.STAGED, E.DROP),
        (S.STAGED, E.PROMOTE),
        (S.ENROLLED, E.SEAT_RESERVED),
        (S.ENROLLED, E.PROMOTE),
        (S.WAITLISTED, E.SEAT_RESERVED),
        (S.DROPPED, E.DROP),
        (S.DROPPED, E.PROMOTE),
        (S.REJECTED, E.SEAT_RESERVED),
    ],
)
def test_forbidden_transitions_raise(status, event):
    with pytest.raises(InvalidTransition):
        next_status(status, event)


def test_sources_for_drives_conditional_sql():
    assert set(sources_for(E.DROP)) == {"ENROLLED", "WAITLISTED"}
    assert sources_for(E.PROMOTE) == ("WAITLISTED",)
    assert sources_for(E.SEAT_RESERVED) == ("STAGED",)
    assert sources_for(E.RESERVATION_FAILED) == ("STAGED",)


def test_active_and_terminal_partition_all_used_statuses():
    assert set(ACTIVE_STATUSES) == {"STAGED", "ENROLLED", "WAITLISTED"}
    assert set(TERMINAL_STATUSES) == {"DROPPED", "REJECTED"}


def test_offered_is_not_part_of_the_auto_promote_lifecycle():
    with pytest.raises(ValueError):
        state_of("OFFERED")
