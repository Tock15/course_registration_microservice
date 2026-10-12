"""Enrollment API integration tests."""
from enrollment_service.tests.conftest import EnrollmentTestEnv


async def test_enroll_success_returns_201_and_holds_catalog_seat(env: EnrollmentTestEnv):
    res = await env.enroll("STU_001", 101)

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "ENROLLED"
    assert body["course_code"] == "CS301"
    assert body["enrollment_id"] is not None
    assert env.upstreams.seats_held(101) == 1
    assert env.publisher.types() == ["ENROLLMENT_CONFIRMED"]

    mine = await env.mine("STU_001")
    assert mine["total_credits"] == 3
    assert [e["status"] for e in mine["enrollments"]] == ["ENROLLED"]
    assert mine["enrollments"][0]["schedule_days"] == ["MON", "WED"]


async def test_identity_comes_from_header_not_body(env: EnrollmentTestEnv):
    missing = await env.client.post("/enrollments", json={"section_id": 101})
    assert missing.status_code == 401

    spoofed = await env.client.post(
        "/enrollments",
        json={"section_id": 101, "student_id": "STU_002"},
        headers={"X-Student-Id": "STU_001"},
    )
    assert spoofed.status_code == 201
    assert spoofed.json()["student_id"] == "STU_001"
    assert await env.status_of("STU_002", 101) is None


async def test_unknown_section_or_student_returns_404(env: EnrollmentTestEnv):
    assert (await env.enroll("STU_001", 999)).status_code == 404
    assert (await env.enroll("STU_GHOST", 101)).status_code == 404


async def test_missing_prerequisites_rejected_400(env: EnrollmentTestEnv):
    res = await env.enroll("STU_NEW", 101)

    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "REJECTED"
    assert body["rule_name"] == "PrerequisiteRule"
    assert "CS102" in body["message"] and "CS202" in body["message"]
    assert env.upstreams.seats_held(101) == 0
    assert await env.rows() == []


async def test_financial_hold_rejected_400(env: EnrollmentTestEnv):
    res = await env.enroll("STU_HOLD", 103)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "StudentEligibilityRule"


async def test_credit_cap_rejected_400(env: EnrollmentTestEnv):
    env.upstreams.add_student("STU_CAP", completed=("CS101",), max_credits=5)
    assert (await env.enroll("STU_CAP", 104)).status_code == 201

    res = await env.enroll("STU_CAP", 105)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "CreditCapRule"


async def test_time_conflict_rejected_400(env: EnrollmentTestEnv):
    assert (await env.enroll("STU_001", 101)).status_code == 201

    res = await env.enroll("STU_001", 106)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "TimeConflictRule"


async def test_second_section_of_same_course_rejected(env: EnrollmentTestEnv):
    assert (await env.enroll("STU_001", 101)).status_code == 201

    again = await env.enroll("STU_001", 101)
    other = await env.enroll("STU_001", 102)
    assert again.status_code == 400 and other.status_code == 400
    assert again.json()["rule_name"] == other.json()["rule_name"] == "DuplicateEnrollmentRule"
    assert env.upstreams.seats_held(101) == 1


async def test_full_section_waitlists_with_fifo_positions(env: EnrollmentTestEnv):
    assert (await env.enroll("STU_001", 105)).status_code == 201

    second = await env.enroll("STU_002", 105)
    third = await env.enroll("STU_003", 105)

    assert second.status_code == 409 and third.status_code == 409
    assert second.json()["status"] == "WAITLISTED"
    assert second.json()["waitlist_position"] == 1
    assert third.json()["waitlist_position"] == 2
    reserve_calls = [c for c in env.upstreams.seat_calls if c[0] == "reserve"]
    assert len(reserve_calls) == 2
    assert env.publisher.types().count("WAITLIST_JOINED") == 2

    mine = await env.mine("STU_003")
    assert mine["enrollments"][0]["waitlist_position"] == 2
    assert mine["total_credits"] == 0


async def test_drop_passes_seat_to_waitlist_head_without_catalog_call(env: EnrollmentTestEnv):
    await env.enroll("STU_001", 105)
    await env.enroll("STU_002", 105)
    await env.enroll("STU_003", 105)
    calls_before = len(env.upstreams.seat_calls)
    seat_key = next(r.seat_key for r in await env.rows(105) if r.student_id == "STU_001")

    res = await env.drop("STU_001", 105)

    assert res.status_code == 200
    assert res.json()["status"] == "DROPPED"
    assert "passed to the next waitlisted student" in res.json()["message"]
    assert len(env.upstreams.seat_calls) == calls_before
    assert env.upstreams.seats_held(105) == 1

    rows = {r.student_id: r for r in await env.rows(105)}
    assert rows["STU_001"].status == "DROPPED" and rows["STU_001"].seat_key is None
    assert rows["STU_002"].status == "ENROLLED" and rows["STU_002"].seat_key == seat_key
    assert (await env.mine("STU_003"))["enrollments"][0]["waitlist_position"] == 1
    assert env.publisher.types()[-2:] == ["COURSE_DROPPED", "ENROLLMENT_CONFIRMED"]
    assert env.publisher.events[-1].student_id == "STU_002"


async def test_drop_without_waitlist_releases_seat_at_catalog(env: EnrollmentTestEnv):
    await env.enroll("STU_001", 103)
    assert env.upstreams.seats_held(103) == 1

    res = await env.drop("STU_001", 103)

    assert res.status_code == 200
    assert env.upstreams.seats_held(103) == 0
    assert [r.seat_key for r in await env.rows(103)] == [None]
    assert (await env.enroll("STU_001", 103)).status_code == 201


async def test_leaving_waitlist_shifts_queue(env: EnrollmentTestEnv):
    await env.enroll("STU_001", 105)
    await env.enroll("STU_002", 105)
    await env.enroll("STU_003", 105)

    res = await env.drop("STU_002", 105)

    assert res.status_code == 200
    assert "Removed from the waitlist" in res.json()["message"]
    assert (await env.mine("STU_003"))["enrollments"][0]["waitlist_position"] == 1
    assert env.upstreams.seats_held(105) == 1


async def test_drop_when_not_enrolled_returns_404(env: EnrollmentTestEnv):
    assert (await env.drop("STU_001", 101)).status_code == 404


async def test_catalog_outage_during_reserve_returns_503_and_compensates(
    env: EnrollmentTestEnv,
):
    env.upstreams.reserve_down = True

    res = await env.enroll("STU_001", 103)

    assert res.status_code == 503
    [row] = await env.rows(103)
    assert row.status == "REJECTED"
    assert row.seat_key is None
    assert list(env.upstreams.reservations.values()) == ["CANCELLED"]
    assert env.upstreams.seats_held(103) == 0

    env.upstreams.reserve_down = False
    assert (await env.enroll("STU_001", 103)).status_code == 201


async def test_student_service_outage_returns_503(env: EnrollmentTestEnv):
    env.upstreams.student_down = True
    res = await env.enroll("STU_001", 101)
    assert res.status_code == 503
    assert "student_service" in res.json()["detail"]
    assert await env.rows() == []
