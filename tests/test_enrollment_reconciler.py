"""Reconciler tests."""
from datetime import timedelta

from common.schemas import EnrollmentStatus
from enrollment_service import repository
from enrollment_service.models import utcnow
from enrollment_service.reconciler import Reconciler
from tests.enrollment_support import EnrollmentTestEnv


def make_reconciler(enrollment_env: EnrollmentTestEnv) -> Reconciler:
    return Reconciler(enrollment_env.sessions, enrollment_env.catalog, enrollment_env.publisher, stale_after_seconds=60)


async def test_stale_staged_row_is_rejected_and_its_seat_released(enrollment_env: EnrollmentTestEnv):
    section = await enrollment_env.catalog.get_section(103)
    async with enrollment_env.sessions() as session, session.begin():
        row = await repository.add_enrollment(
            session,
            student_id="STU_001",
            section=section,
            credits=3,
            status=EnrollmentStatus.STAGED,
            seat_key="crashed-key",
        )
        row.created_at = utcnow() - timedelta(minutes=5)
    assert await enrollment_env.catalog.reserve_seat(103, "crashed-key")
    assert enrollment_env.upstreams.seats_held(103) == 1

    report = await make_reconciler(enrollment_env).run_once()

    assert report.stale_rejected == 1 and report.released == 1
    [row] = await enrollment_env.rows(103)
    assert row.status == "REJECTED" and row.seat_key is None
    assert enrollment_env.upstreams.seats_held(103) == 0


async def test_fresh_staged_row_is_left_alone(enrollment_env: EnrollmentTestEnv):
    section = await enrollment_env.catalog.get_section(103)
    async with enrollment_env.sessions() as session, session.begin():
        await repository.add_enrollment(
            session,
            student_id="STU_001",
            section=section,
            credits=3,
            status=EnrollmentStatus.STAGED,
            seat_key="in-flight-key",
        )

    report = await make_reconciler(enrollment_env).run_once()

    assert report.stale_rejected == 0
    assert (await enrollment_env.rows(103))[0].status == "STAGED"


async def test_failed_release_is_retried_until_catalog_recovers(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 103)
    enrollment_env.upstreams.catalog_down = True

    res = await enrollment_env.drop("STU_001", 103)
    assert res.status_code == 200
    [row] = await enrollment_env.rows(103)
    assert row.status == "DROPPED" and row.seat_key is not None
    assert enrollment_env.upstreams.seats_held(103) == 1

    reconciler = make_reconciler(enrollment_env)
    assert (await reconciler.run_once()).released == 0

    enrollment_env.upstreams.catalog_down = False
    assert (await reconciler.run_once()).released == 1
    assert enrollment_env.upstreams.seats_held(103) == 0
    assert (await enrollment_env.rows(103))[0].seat_key is None


async def test_capacity_increase_is_pumped_into_waitlist_in_fifo_order(
    enrollment_env: EnrollmentTestEnv,
):
    for student in ("STU_001", "STU_002", "STU_003", "STU_004"):
        await enrollment_env.enroll(student, 105)
    enrollment_env.upstreams.set_capacity(105, 3)

    report = await make_reconciler(enrollment_env).run_once()

    assert report.offered == 2
    assert enrollment_env.upstreams.seats_held(105) == 3
    assert await enrollment_env.status_of("STU_002", 105) == "OFFERED"
    assert await enrollment_env.status_of("STU_003", 105) == "OFFERED"
    assert await enrollment_env.status_of("STU_004", 105) == "WAITLISTED"
    assert (await enrollment_env.mine("STU_004"))["enrollments"][0]["waitlist_position"] == 1
    assert enrollment_env.publisher.types()[-2:] == ["WAITLIST_SLOT_OFFERED"] * 2
    offered = [e.student_id for e in enrollment_env.publisher.events[-2:]]
    assert offered == ["STU_002", "STU_003"]


async def test_pump_does_nothing_when_section_still_full(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 105)
    await enrollment_env.enroll("STU_002", 105)

    report = await make_reconciler(enrollment_env).run_once()

    assert report.offered == 0
    assert await enrollment_env.status_of("STU_002", 105) == "WAITLISTED"
    assert enrollment_env.upstreams.seats_held(105) == 1


async def make_offer(enrollment_env: EnrollmentTestEnv, *waiting: str) -> int:
    await enrollment_env.enroll("STU_001", 105)
    for student in waiting:
        await enrollment_env.enroll(student, 105)
    await enrollment_env.drop("STU_001", 105)
    return (await enrollment_env.row_of(waiting[0], 105)).id


async def test_expired_offer_cascades_same_seat_to_next_in_line(enrollment_env: EnrollmentTestEnv):
    offer_id = await make_offer(enrollment_env, "STU_002", "STU_003")
    seat_key = (await enrollment_env.row_of("STU_002", 105)).seat_key
    await enrollment_env.backdate_offer(offer_id)
    calls_before = len(enrollment_env.upstreams.seat_calls)

    report = await make_reconciler(enrollment_env).run_once()

    assert (report.expired, report.released, report.offered) == (1, 0, 0)
    expired = await enrollment_env.row_of("STU_002", 105)
    assert expired.status == "DROPPED" and expired.seat_key is None
    assert expired.reason == "Seat offer expired without a claim; seat passed to the waitlist."
    nxt = await enrollment_env.row_of("STU_003", 105)
    assert nxt.status == "OFFERED" and nxt.seat_key == seat_key
    assert len(enrollment_env.upstreams.seat_calls) == calls_before
    assert enrollment_env.upstreams.seats_held(105) == 1
    assert enrollment_env.publisher.types()[-1] == "WAITLIST_SLOT_OFFERED"
    assert enrollment_env.publisher.events[-1].student_id == "STU_003"
    assert (await enrollment_env.claim("STU_002", offer_id)).json()["status"] == (
        "EXPIRED_OR_ALREADY_CLAIMED"
    )


async def test_expired_offer_with_empty_queue_releases_seat(enrollment_env: EnrollmentTestEnv):
    offer_id = await make_offer(enrollment_env, "STU_002")
    await enrollment_env.backdate_offer(offer_id)

    report = await make_reconciler(enrollment_env).run_once()

    assert (report.expired, report.released) == (1, 1)
    expired = await enrollment_env.row_of("STU_002", 105)
    assert expired.status == "DROPPED" and expired.seat_key is None
    assert enrollment_env.upstreams.seats_held(105) == 0


async def test_live_offer_is_left_alone(enrollment_env: EnrollmentTestEnv):
    await make_offer(enrollment_env, "STU_002", "STU_003")

    report = await make_reconciler(enrollment_env).run_once()

    assert report.expired == 0 and not report.changed
    assert await enrollment_env.status_of("STU_002", 105) == "OFFERED"
    assert await enrollment_env.status_of("STU_003", 105) == "WAITLISTED"
