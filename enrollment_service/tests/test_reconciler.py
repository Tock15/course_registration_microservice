"""Reconciler tests."""
from datetime import timedelta

from common.schemas import EnrollmentStatus
from enrollment_service import repository
from enrollment_service.models import utcnow
from enrollment_service.reconciler import Reconciler
from enrollment_service.tests.conftest import EnrollmentTestEnv


def make_reconciler(env: EnrollmentTestEnv) -> Reconciler:
    return Reconciler(env.sessions, env.catalog, env.publisher, stale_after_seconds=60)


async def test_stale_staged_row_is_rejected_and_its_seat_released(env: EnrollmentTestEnv):
    section = await env.catalog.get_section(103)
    async with env.sessions() as session, session.begin():
        row = await repository.add_enrollment(
            session,
            student_id="STU_001",
            section=section,
            credits=3,
            status=EnrollmentStatus.STAGED,
            seat_key="crashed-key",
        )
        row.created_at = utcnow() - timedelta(minutes=5)
    assert await env.catalog.reserve_seat(103, "crashed-key")
    assert env.upstreams.seats_held(103) == 1

    report = await make_reconciler(env).run_once()

    assert report.stale_rejected == 1 and report.released == 1
    [row] = await env.rows(103)
    assert row.status == "REJECTED" and row.seat_key is None
    assert env.upstreams.seats_held(103) == 0


async def test_fresh_staged_row_is_left_alone(env: EnrollmentTestEnv):
    section = await env.catalog.get_section(103)
    async with env.sessions() as session, session.begin():
        await repository.add_enrollment(
            session,
            student_id="STU_001",
            section=section,
            credits=3,
            status=EnrollmentStatus.STAGED,
            seat_key="in-flight-key",
        )

    report = await make_reconciler(env).run_once()

    assert report.stale_rejected == 0
    assert (await env.rows(103))[0].status == "STAGED"


async def test_failed_release_is_retried_until_catalog_recovers(env: EnrollmentTestEnv):
    await env.enroll("STU_001", 103)
    env.upstreams.catalog_down = True

    res = await env.drop("STU_001", 103)
    assert res.status_code == 200
    [row] = await env.rows(103)
    assert row.status == "DROPPED" and row.seat_key is not None
    assert env.upstreams.seats_held(103) == 1

    reconciler = make_reconciler(env)
    assert (await reconciler.run_once()).released == 0

    env.upstreams.catalog_down = False
    assert (await reconciler.run_once()).released == 1
    assert env.upstreams.seats_held(103) == 0
    assert (await env.rows(103))[0].seat_key is None


async def test_capacity_increase_is_pumped_into_waitlist_in_fifo_order(
    env: EnrollmentTestEnv,
):
    for student in ("STU_001", "STU_002", "STU_003", "STU_004"):
        await env.enroll(student, 105)
    env.upstreams.set_capacity(105, 3)

    report = await make_reconciler(env).run_once()

    assert report.promoted == 2
    assert env.upstreams.seats_held(105) == 3
    assert await env.status_of("STU_002", 105) == "ENROLLED"
    assert await env.status_of("STU_003", 105) == "ENROLLED"
    assert await env.status_of("STU_004", 105) == "WAITLISTED"
    assert (await env.mine("STU_004"))["enrollments"][0]["waitlist_position"] == 1
    promoted = [e.student_id for e in env.publisher.events[-2:]]
    assert promoted == ["STU_002", "STU_003"]


async def test_pump_does_nothing_when_section_still_full(env: EnrollmentTestEnv):
    await env.enroll("STU_001", 105)
    await env.enroll("STU_002", 105)

    report = await make_reconciler(env).run_once()

    assert report.promoted == 0
    assert await env.status_of("STU_002", 105) == "WAITLISTED"
    assert env.upstreams.seats_held(105) == 1
