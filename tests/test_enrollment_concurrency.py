"""Concurrency tests: zero overbooking and FIFO under burst load."""
import asyncio

from enrollment_service import waitlist
from tests.enrollment_support import EnrollmentTestEnv

SENIOR = ("CS101", "CS102", "CS202")


async def test_100_concurrent_requests_10_seats_zero_overbooking(enrollment_env: EnrollmentTestEnv):
    total, capacity = 100, 10
    students = [f"STU_{i:03d}" for i in range(total)]
    for student in students:
        enrollment_env.upstreams.add_student(student, completed=SENIOR)

    responses = await asyncio.gather(*(enrollment_env.enroll(s, 101) for s in students))

    enrolled = [r for r in responses if r.status_code == 201 and r.json()["status"] == "ENROLLED"]
    waitlisted = [
        r for r in responses if r.status_code == 409 and r.json()["status"] == "WAITLISTED"
    ]
    assert len(enrolled) == capacity, [r.status_code for r in responses]
    assert len(waitlisted) == total - capacity

    rows = await enrollment_env.rows(101)
    assert enrollment_env.upstreams.seats_held(101) == capacity
    assert sorted({r.status for r in rows}) == ["ENROLLED", "WAITLISTED"]
    held_keys = [r.seat_key for r in rows if r.status == "ENROLLED"]
    assert len(set(held_keys)) == capacity
    assert all(enrollment_env.upstreams.reservations[k] == "ACTIVE" for k in held_keys)

    async with enrollment_env.sessions() as session, session.begin():
        positions = [
            await waitlist.position(session, r.id) for r in rows if r.status == "WAITLISTED"
        ]
    assert sorted(positions) == list(range(1, total - capacity + 1))


async def test_double_click_burst_creates_exactly_one_enrollment(enrollment_env: EnrollmentTestEnv):
    responses = await asyncio.gather(*(enrollment_env.enroll("STU_001", 103) for _ in range(8)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 400, 409}
    rows = await enrollment_env.rows(103)
    assert [r.status for r in rows] == ["ENROLLED"]
    assert enrollment_env.upstreams.seats_held(103) == 1


async def test_concurrent_drops_and_enrolls_keep_fifo_and_seat_invariant(
    enrollment_env: EnrollmentTestEnv,
):
    first_wave = [f"STU_A{i:02d}" for i in range(15)]
    second_wave = [f"STU_B{i:02d}" for i in range(5)]
    for student in first_wave + second_wave:
        enrollment_env.upstreams.add_student(student, completed=SENIOR)

    for student in first_wave:
        await enrollment_env.enroll(student, 101)

    await asyncio.gather(
        *(enrollment_env.drop(s, 101) for s in first_wave[:5]),
        *(enrollment_env.enroll(s, 101) for s in second_wave),
    )

    status = {r.student_id: r.status for r in await enrollment_env.rows(101)}
    holders = sorted(s for s, st in status.items() if st in ("ENROLLED", "OFFERED"))
    assert holders == sorted(first_wave[5:])
    assert all(status[s] == "ENROLLED" for s in first_wave[5:10])
    assert all(status[s] == "OFFERED" for s in first_wave[10:])
    assert all(status[s] == "WAITLISTED" for s in second_wave)
    assert enrollment_env.upstreams.seats_held(101) == 10 == len(holders)


async def test_claim_burst_on_one_offer_enrolls_exactly_once(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 105)
    await enrollment_env.enroll("STU_002", 105)
    await enrollment_env.drop("STU_001", 105)
    offer_id = (await enrollment_env.row_of("STU_002", 105)).id

    responses = await asyncio.gather(
        *(enrollment_env.claim("STU_002", offer_id) for _ in range(10))
    )

    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["CLAIMED"] + ["EXPIRED_OR_ALREADY_CLAIMED"] * 9
    assert await enrollment_env.status_of("STU_002", 105) == "ENROLLED"
    assert enrollment_env.publisher.types().count("ENROLLMENT_CONFIRMED") == 2
    assert enrollment_env.upstreams.seats_held(105) == 1
