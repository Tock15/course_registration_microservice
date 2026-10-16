"""Enrollment API integration tests."""
from datetime import datetime, timedelta

from enrollment_service.models import utcnow
from tests.enrollment_support import EnrollmentTestEnv


async def test_enroll_success_returns_201_and_holds_catalog_seat(enrollment_env: EnrollmentTestEnv):
    res = await enrollment_env.enroll("STU_001", 101)

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "ENROLLED"
    assert body["course_code"] == "CS301"
    assert body["enrollment_id"] is not None
    assert enrollment_env.upstreams.seats_held(101) == 1
    assert enrollment_env.publisher.types() == ["ENROLLMENT_CONFIRMED"]

    mine = await enrollment_env.mine("STU_001")
    assert mine["total_credits"] == 3
    assert [e["status"] for e in mine["enrollments"]] == ["ENROLLED"]
    assert mine["enrollments"][0]["schedule_days"] == ["MON", "WED"]


async def test_identity_comes_from_header_not_body(enrollment_env: EnrollmentTestEnv):
    missing = await enrollment_env.client.post("/enrollments", json={"section_id": 101})
    assert missing.status_code == 401

    spoofed = await enrollment_env.client.post(
        "/enrollments",
        json={"section_id": 101, "student_id": "STU_002"},
        headers={"X-Student-Id": "STU_001"},
    )
    assert spoofed.status_code == 201
    assert spoofed.json()["student_id"] == "STU_001"
    assert await enrollment_env.status_of("STU_002", 101) is None


async def test_unknown_section_or_student_returns_404(enrollment_env: EnrollmentTestEnv):
    assert (await enrollment_env.enroll("STU_001", 999)).status_code == 404
    assert (await enrollment_env.enroll("STU_GHOST", 101)).status_code == 404


async def test_missing_prerequisites_rejected_400(enrollment_env: EnrollmentTestEnv):
    res = await enrollment_env.enroll("STU_NEW", 101)

    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "REJECTED"
    assert body["rule_name"] == "PrerequisiteRule"
    assert "CS102" in body["message"] and "CS202" in body["message"]
    assert enrollment_env.upstreams.seats_held(101) == 0
    assert await enrollment_env.rows() == []


async def test_financial_hold_rejected_400(enrollment_env: EnrollmentTestEnv):
    res = await enrollment_env.enroll("STU_HOLD", 103)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "StudentEligibilityRule"


async def test_credit_cap_rejected_400(enrollment_env: EnrollmentTestEnv):
    enrollment_env.upstreams.add_student("STU_CAP", completed=("CS101",), max_credits=5)
    assert (await enrollment_env.enroll("STU_CAP", 104)).status_code == 201

    res = await enrollment_env.enroll("STU_CAP", 105)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "CreditCapRule"


async def test_time_conflict_rejected_400(enrollment_env: EnrollmentTestEnv):
    assert (await enrollment_env.enroll("STU_001", 101)).status_code == 201

    res = await enrollment_env.enroll("STU_001", 106)
    assert res.status_code == 400
    assert res.json()["rule_name"] == "TimeConflictRule"


async def test_second_section_of_same_course_rejected(enrollment_env: EnrollmentTestEnv):
    assert (await enrollment_env.enroll("STU_001", 101)).status_code == 201

    again = await enrollment_env.enroll("STU_001", 101)
    other = await enrollment_env.enroll("STU_001", 102)
    assert again.status_code == 400 and other.status_code == 400
    assert again.json()["rule_name"] == other.json()["rule_name"] == "DuplicateEnrollmentRule"
    assert enrollment_env.upstreams.seats_held(101) == 1


async def test_full_section_waitlists_with_fifo_positions(enrollment_env: EnrollmentTestEnv):
    assert (await enrollment_env.enroll("STU_001", 105)).status_code == 201

    second = await enrollment_env.enroll("STU_002", 105)
    third = await enrollment_env.enroll("STU_003", 105)

    assert second.status_code == 409 and third.status_code == 409
    assert second.json()["status"] == "WAITLISTED"
    assert second.json()["waitlist_position"] == 1
    assert third.json()["waitlist_position"] == 2
    reserve_calls = [c for c in enrollment_env.upstreams.seat_calls if c[0] == "reserve"]
    assert len(reserve_calls) == 2
    assert enrollment_env.publisher.types().count("WAITLIST_JOINED") == 2

    mine = await enrollment_env.mine("STU_003")
    assert mine["enrollments"][0]["waitlist_position"] == 2
    assert mine["total_credits"] == 0


async def test_drop_offers_seat_to_waitlist_head_without_catalog_call(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 105)
    await enrollment_env.enroll("STU_002", 105)
    await enrollment_env.enroll("STU_003", 105)
    calls_before = len(enrollment_env.upstreams.seat_calls)
    seat_key = next(r.seat_key for r in await enrollment_env.rows(105) if r.student_id == "STU_001")

    res = await enrollment_env.drop("STU_001", 105)

    assert res.status_code == 200
    assert res.json()["status"] == "DROPPED"
    assert "offered to the next waitlisted student" in res.json()["message"]
    assert len(enrollment_env.upstreams.seat_calls) == calls_before
    assert enrollment_env.upstreams.seats_held(105) == 1

    rows = {r.student_id: r for r in await enrollment_env.rows(105)}
    assert rows["STU_001"].status == "DROPPED" and rows["STU_001"].seat_key is None
    offered = rows["STU_002"]
    assert offered.status == "OFFERED" and offered.seat_key == seat_key
    ttl = offered.offer_expires_at - utcnow()
    assert timedelta(hours=23, minutes=59) < ttl <= timedelta(hours=24)
    assert (await enrollment_env.mine("STU_003"))["enrollments"][0]["waitlist_position"] == 1
    assert enrollment_env.publisher.types()[-2:] == ["COURSE_DROPPED", "WAITLIST_SLOT_OFFERED"]
    event = enrollment_env.publisher.events[-1]
    assert (event.student_id, event.offer_id, event.section_id) == ("STU_002", offered.id, 105)
    assert event.expires_at == offered.offer_expires_at


async def offer_seat_to_stu_002(enrollment_env: EnrollmentTestEnv) -> int:
    await enrollment_env.enroll("STU_001", 105)
    await enrollment_env.enroll("STU_002", 105)
    await enrollment_env.drop("STU_001", 105)
    [entry] = await enrollment_env.my_waitlist("STU_002")
    assert entry["status"] == "OFFERED"
    return entry["offer_id"]


async def test_claim_enrolls_offered_student_without_catalog_call(enrollment_env: EnrollmentTestEnv):
    offer_id = await offer_seat_to_stu_002(enrollment_env)
    calls_before = len(enrollment_env.upstreams.seat_calls)

    res = await enrollment_env.claim("STU_002", offer_id)

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "CLAIMED"
    assert (body["enrollment_id"], body["section_id"], body["course_code"]) == (offer_id, 105, "CS202")
    assert await enrollment_env.status_of("STU_002", 105) == "ENROLLED"
    assert len(enrollment_env.upstreams.seat_calls) == calls_before
    assert enrollment_env.upstreams.seats_held(105) == 1
    assert enrollment_env.publisher.types()[-1] == "ENROLLMENT_CONFIRMED"
    assert enrollment_env.publisher.events[-1].student_id == "STU_002"
    assert await enrollment_env.my_waitlist("STU_002") == []
    assert (await enrollment_env.mine("STU_002"))["total_credits"] == 3


async def test_second_claim_reports_already_claimed(enrollment_env: EnrollmentTestEnv):
    offer_id = await offer_seat_to_stu_002(enrollment_env)

    first = await enrollment_env.claim("STU_002", offer_id)
    again = await enrollment_env.claim("STU_002", offer_id)

    assert first.json()["status"] == "CLAIMED"
    assert again.status_code == 200
    assert again.json()["status"] == "EXPIRED_OR_ALREADY_CLAIMED"
    assert enrollment_env.publisher.types().count("ENROLLMENT_CONFIRMED") == 2


async def test_expired_offer_cannot_be_claimed(enrollment_env: EnrollmentTestEnv):
    offer_id = await offer_seat_to_stu_002(enrollment_env)
    await enrollment_env.backdate_offer(offer_id)

    res = await enrollment_env.claim("STU_002", offer_id)

    assert res.status_code == 200
    assert res.json()["status"] == "EXPIRED_OR_ALREADY_CLAIMED"
    assert await enrollment_env.status_of("STU_002", 105) == "OFFERED"


async def test_only_the_offered_student_can_claim(enrollment_env: EnrollmentTestEnv):
    offer_id = await offer_seat_to_stu_002(enrollment_env)

    stolen = await enrollment_env.claim("STU_003", offer_id)
    unknown = await enrollment_env.claim("STU_002", 9999)
    anonymous = await enrollment_env.client.post("/waitlist/claim", json={"offer_id": offer_id})

    assert stolen.json()["status"] == unknown.json()["status"] == "EXPIRED_OR_ALREADY_CLAIMED"
    assert anonymous.status_code == 401
    assert await enrollment_env.status_of("STU_002", 105) == "OFFERED"


async def test_declining_offer_passes_seat_to_next_in_line(enrollment_env: EnrollmentTestEnv):
    for student in ("STU_001", "STU_002", "STU_003"):
        await enrollment_env.enroll(student, 105)
    await enrollment_env.drop("STU_001", 105)
    seat_key = (await enrollment_env.row_of("STU_002", 105)).seat_key

    res = await enrollment_env.drop("STU_002", 105)

    assert res.status_code == 200
    assert res.json()["message"] == (
        "Declined the seat offer for section 105. "
        "The seat was offered to the next waitlisted student."
    )
    declined = await enrollment_env.row_of("STU_002", 105)
    assert declined.status == "DROPPED" and declined.seat_key is None
    nxt = await enrollment_env.row_of("STU_003", 105)
    assert nxt.status == "OFFERED" and nxt.seat_key == seat_key
    assert enrollment_env.upstreams.seats_held(105) == 1
    assert enrollment_env.publisher.types()[-1] == "WAITLIST_SLOT_OFFERED"
    assert enrollment_env.publisher.events[-1].student_id == "STU_003"


async def test_declining_offer_with_empty_queue_releases_seat(enrollment_env: EnrollmentTestEnv):
    await offer_seat_to_stu_002(enrollment_env)

    res = await enrollment_env.drop("STU_002", 105)

    assert res.json()["message"] == "Declined the seat offer for section 105."
    declined = await enrollment_env.row_of("STU_002", 105)
    assert declined.status == "DROPPED" and declined.seat_key is None
    assert enrollment_env.upstreams.seats_held(105) == 0


async def test_waitlist_me_lists_queue_positions_and_offers(enrollment_env: EnrollmentTestEnv):
    for student in ("STU_001", "STU_002", "STU_003"):
        await enrollment_env.enroll(student, 105)
    stu_003 = await enrollment_env.row_of("STU_003", 105)

    assert await enrollment_env.my_waitlist("STU_003") == [
        {
            "offer_id": stu_003.id,
            "section_id": 105,
            "course_code": "CS202",
            "position": 2,
            "status": "WAITLISTED",
            "expires_at": None,
        }
    ]
    assert await enrollment_env.my_waitlist("STU_001") == []

    await enrollment_env.drop("STU_001", 105)

    [offer] = await enrollment_env.my_waitlist("STU_002")
    assert (offer["status"], offer["position"]) == ("OFFERED", 1)
    expires_at = (await enrollment_env.row_of("STU_002", 105)).offer_expires_at
    assert datetime.fromisoformat(offer["expires_at"]) == expires_at
    assert (await enrollment_env.my_waitlist("STU_003"))[0]["position"] == 1
    [record] = (await enrollment_env.mine("STU_002"))["enrollments"]
    assert record["status"] == "OFFERED" and record["offer_expires_at"] == offer["expires_at"]


async def test_enrollments_me_returns_plain_array(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 101)
    await enrollment_env.enroll("STU_001", 103)
    await enrollment_env.drop("STU_001", 103)

    res = await enrollment_env.client.get("/enrollments/me", headers={"X-Student-Id": "STU_001"})

    assert res.status_code == 200
    assert sorted((e["section_id"], e["status"]) for e in res.json()) == [
        (101, "ENROLLED"),
        (103, "DROPPED"),
    ]


async def test_drop_without_waitlist_releases_seat_at_catalog(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 103)
    assert enrollment_env.upstreams.seats_held(103) == 1

    res = await enrollment_env.drop("STU_001", 103)

    assert res.status_code == 200
    assert enrollment_env.upstreams.seats_held(103) == 0
    assert [r.seat_key for r in await enrollment_env.rows(103)] == [None]
    assert (await enrollment_env.enroll("STU_001", 103)).status_code == 201


async def test_leaving_waitlist_shifts_queue(enrollment_env: EnrollmentTestEnv):
    await enrollment_env.enroll("STU_001", 105)
    await enrollment_env.enroll("STU_002", 105)
    await enrollment_env.enroll("STU_003", 105)

    res = await enrollment_env.drop("STU_002", 105)

    assert res.status_code == 200
    assert "Removed from the waitlist" in res.json()["message"]
    assert (await enrollment_env.mine("STU_003"))["enrollments"][0]["waitlist_position"] == 1
    assert enrollment_env.upstreams.seats_held(105) == 1


async def test_drop_when_not_enrolled_returns_404(enrollment_env: EnrollmentTestEnv):
    assert (await enrollment_env.drop("STU_001", 101)).status_code == 404


async def test_catalog_outage_during_reserve_returns_503_and_compensates(
    enrollment_env: EnrollmentTestEnv,
):
    enrollment_env.upstreams.reserve_down = True

    res = await enrollment_env.enroll("STU_001", 103)

    assert res.status_code == 503
    [row] = await enrollment_env.rows(103)
    assert row.status == "REJECTED"
    assert row.seat_key is None
    assert list(enrollment_env.upstreams.reservations.values()) == ["CANCELLED"]
    assert enrollment_env.upstreams.seats_held(103) == 0

    enrollment_env.upstreams.reserve_down = False
    assert (await enrollment_env.enroll("STU_001", 103)).status_code == 201


async def test_student_service_outage_returns_503(enrollment_env: EnrollmentTestEnv):
    enrollment_env.upstreams.student_down = True
    res = await enrollment_env.enroll("STU_001", 101)
    assert res.status_code == 503
    assert "student_service" in res.json()["detail"]
    assert await enrollment_env.rows() == []
