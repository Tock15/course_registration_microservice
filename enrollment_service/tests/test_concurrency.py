"""
enrollment_service/tests/test_concurrency.py
In-process concurrency tests: burst traffic against one section must never
overbook, must keep enrollment.db consistent with Catalog's seat counter, and
must preserve FIFO order on the waitlist.
"""
import asyncio

from enrollment_service import waitlist
from enrollment_service.tests.conftest import EnrollmentTestEnv

SENIOR = ("CS101", "CS102", "CS202")


async def test_100_concurrent_requests_10_seats_zero_overbooking(env: EnrollmentTestEnv):
    total, capacity = 100, 10
    students = [f"STU_{i:03d}" for i in range(total)]
    for student in students:
        env.upstreams.add_student(student, completed=SENIOR)

    responses = await asyncio.gather(*(env.enroll(s, 101) for s in students))

    enrolled = [r for r in responses if r.status_code == 201 and r.json()["status"] == "ENROLLED"]
    waitlisted = [
        r for r in responses if r.status_code == 409 and r.json()["status"] == "WAITLISTED"
    ]
    assert len(enrolled) == capacity, [r.status_code for r in responses]
    assert len(waitlisted) == total - capacity

    rows = await env.rows(101)
    assert env.upstreams.seats_held(101) == capacity
    assert sorted({r.status for r in rows}) == ["ENROLLED", "WAITLISTED"]
    held_keys = [r.seat_key for r in rows if r.status == "ENROLLED"]
    assert len(set(held_keys)) == capacity
    assert all(env.upstreams.reservations[k] == "ACTIVE" for k in held_keys)

    async with env.sessions() as session, session.begin():
        positions = [
            await waitlist.position(session, r.id) for r in rows if r.status == "WAITLISTED"
        ]
    assert sorted(positions) == list(range(1, total - capacity + 1))


async def test_double_click_burst_creates_exactly_one_enrollment(env: EnrollmentTestEnv):
    responses = await asyncio.gather(*(env.enroll("STU_001", 103) for _ in range(8)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 400, 409}
    rows = await env.rows(103)
    assert [r.status for r in rows] == ["ENROLLED"]
    assert env.upstreams.seats_held(103) == 1


async def test_concurrent_drops_and_enrolls_keep_fifo_and_seat_invariant(
    env: EnrollmentTestEnv,
):
    first_wave = [f"STU_A{i:02d}" for i in range(15)]
    second_wave = [f"STU_B{i:02d}" for i in range(5)]
    for student in first_wave + second_wave:
        env.upstreams.add_student(student, completed=SENIOR)

    for student in first_wave:
        await env.enroll(student, 101)

    await asyncio.gather(
        *(env.drop(s, 101) for s in first_wave[:5]),
        *(env.enroll(s, 101) for s in second_wave),
    )

    status = {r.student_id: r.status for r in await env.rows(101)}
    enrolled = sorted(s for s, st in status.items() if st == "ENROLLED")
    assert enrolled == sorted(first_wave[5:])
    assert all(status[s] == "WAITLISTED" for s in second_wave)
    assert env.upstreams.seats_held(101) == 10 == len(enrolled)
