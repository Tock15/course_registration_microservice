"""
tests/test_catalog_api.py
Integration tests verifying Course Catalog Service REST API endpoints,
search filters, capacity telemetry, and prerequisite validation.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalog_service.database import get_db, init_db, make_engine
from catalog_service.main import app
from enrollment_service.clients import CatalogClient


@pytest.fixture
async def api_client(tmp_path):
    """Isolated test client with database dependency override using WAL and BEGIN IMMEDIATE."""
    test_db_path = tmp_path / "test_catalog.db"
    test_engine = make_engine(f"sqlite+aiosqlite:///{test_db_path.as_posix()}")
    await init_db(test_engine)

    test_session_maker = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with test_session_maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_course_crud_and_search_filters(api_client: AsyncClient):
    client = api_client

    # 1. Create CS101
    res1 = await client.post(
        "/courses",
        json={
            "code": "CS101",
            "title": "Programming Fundamentals",
            "credits": 3,
            "department": "Computer Engineering",
            "prerequisites": [],
        },
    )
    assert res1.status_code == 201
    assert res1.json()["code"] == "CS101"
    assert res1.json()["prerequisites"] == []

    # 2. Create CS102 requiring CS101
    res2 = await client.post(
        "/courses",
        json={
            "code": "CS102",
            "title": "Data Structures",
            "credits": 3,
            "department": "Computer Engineering",
            "prerequisites": ["CS101"],
        },
    )
    assert res2.status_code == 201
    assert res2.json()["prerequisites"] == ["CS101"]

    # 3. Conflict on duplicate course code
    res_dup = await client.post(
        "/courses",
        json={"code": "CS101", "title": "Dup", "credits": 3},
    )
    assert res_dup.status_code == 409

    # 4. Bad request on non-existent prerequisite
    res_bad_prereq = await client.post(
        "/courses",
        json={
            "code": "CS201",
            "title": "Algorithms",
            "credits": 3,
            "prerequisites": ["NON_EXISTENT_999"],
        },
    )
    assert res_bad_prereq.status_code == 400

    # 5. Bad request on cyclic prerequisite definition
    res_cycle = await client.post(
        "/courses",
        json={
            "code": "CS101_CIRCULAR",
            "title": "Cycle Test",
            "credits": 3,
            "prerequisites": ["CS101_CIRCULAR"],
        },
    )
    assert res_cycle.status_code == 400

    # 6. List courses with search filter
    list_res = await client.get("/courses?search=Structures")
    assert list_res.status_code == 200
    courses = list_res.json()
    assert len(courses) == 1
    assert courses[0]["code"] == "CS102"

    # 7. Get specific course by code
    get_res = await client.get("/courses/CS102")
    assert get_res.status_code == 200
    assert get_res.json()["code"] == "CS102"
    assert get_res.json()["prerequisites"] == ["CS101"]

    # 8. 404 for unknown course
    res_404 = await client.get("/courses/UNKNOWN")
    assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_section_management_and_quota_telemetry(api_client: AsyncClient):
    client = api_client

    # Seed course
    await client.post(
        "/courses",
        json={"code": "CS101", "title": "Intro", "credits": 3},
    )

    # 1. Create Section 1 (Capacity 30)
    sec1_res = await client.post(
        "/sections",
        json={
            "course_code": "CS101",
            "section_number": 1,
            "capacity": 30,
            "room": "HM402",
            "schedule_days": ["MON", "WED"],
            "start_time": "09:00",
            "end_time": "10:30",
            "instructor": "Dr. Veera",
        },
    )
    assert sec1_res.status_code == 201
    sec1 = sec1_res.json()
    sec1_id = sec1["id"]
    assert sec1["capacity"] == 30
    assert sec1["enrolled_count"] == 0
    assert sec1["schedule_days"] == ["MON", "WED"]

    # 2. Create Section 2 (Capacity 10, full)
    sec2_res = await client.post(
        "/sections",
        json={
            "course_code": "CS101",
            "section_number": 2,
            "capacity": 10,
            "room": "ECC801",
            "schedule_days": ["TUE", "THU"],
            "instructor": "Faculty Staff",
        },
    )
    assert sec2_res.status_code == 201

    # 3. List sections with day filter
    day_res = await client.get("/sections?day=MON")
    assert day_res.status_code == 200
    assert len(day_res.json()) == 1
    assert day_res.json()[0]["id"] == sec1_id

    # 4. Get section telemetry by id
    get_sec = await client.get(f"/sections/{sec1_id}")
    assert get_sec.status_code == 200
    assert get_sec.json()["room"] == "HM402"

    # 5. Dynamic Quota Expansion (PATCH capacity)
    patch_res = await client.patch(
        f"/sections/{sec1_id}/capacity",
        json={"capacity": 50},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["capacity"] == 50

    # 6. 404 for invalid section
    assert (await client.get("/sections/99999")).status_code == 404
    assert (await client.patch("/sections/99999/capacity", json={"capacity": 100})).status_code == 404


@pytest.mark.asyncio
async def test_prerequisite_validation_endpoint(api_client: AsyncClient):
    client = api_client

    # Create CS101 and CS102 (CS102 requires CS101)
    await client.post(
        "/courses",
        json={"code": "CS101", "title": "Intro", "credits": 3},
    )
    await client.post(
        "/courses",
        json={
            "code": "CS102",
            "title": "Data Structures",
            "credits": 3,
            "prerequisites": ["CS101"],
        },
    )

    # 1. Validation passes when student completed CS101
    pass_res = await client.post(
        "/validate-prereqs",
        json={
            "student_id": "STU_001",
            "course_code": "CS102",
            "completed_courses": ["CS101"],
        },
    )
    assert pass_res.status_code == 200
    body = pass_res.json()
    assert body["is_valid"] is True
    assert body["missing_prereqs"] == []

    # 2. Validation fails when student lacks CS101
    fail_res = await client.post(
        "/validate-prereqs",
        json={
            "student_id": "STU_003",
            "course_code": "CS102",
            "completed_courses": [],
        },
    )
    assert fail_res.status_code == 200
    fail_body = fail_res.json()
    assert fail_body["is_valid"] is False
    assert fail_body["missing_prereqs"] == ["CS101"]

    # 3. Course with no prerequisites passes unconditionally
    noprereq_res = await client.post(
        "/validate-prereqs",
        json={
            "student_id": "STU_003",
            "course_code": "CS101",
            "completed_courses": [],
        },
    )
    assert noprereq_res.status_code == 200
    assert noprereq_res.json()["is_valid"] is True

    # 4. Unknown course returns is_valid=False with informative message
    unknown_res = await client.post(
        "/validate-prereqs",
        json={
            "student_id": "STU_004",
            "course_code": "MATH999",
            "completed_courses": [],
        },
    )
    assert unknown_res.status_code == 200
    assert unknown_res.json()["is_valid"] is False
    assert "does not exist" in unknown_res.json()["message"]

    # 5. Fallback transcript lookup when completed_courses is empty
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "STU_001",
        "completed_courses": ["CS101"],
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        fallback_res = await client.post(
            "/validate-prereqs",
            json={
                "student_id": "STU_001",
                "course_code": "CS102",
                "completed_courses": [],
            },
        )
        assert fallback_res.status_code == 200
        assert fallback_res.json()["is_valid"] is True
        assert fallback_res.json()["missing_prereqs"] == []


@pytest.mark.asyncio
async def test_reserve_and_release_seat_lifecycle_and_idempotency(api_client: AsyncClient):
    client = api_client

    # 1. Setup course and section with capacity 2
    await client.post("/courses", json={"code": "CS101", "title": "Intro", "credits": 3})
    sec_res = await client.post(
        "/sections",
        json={"course_code": "CS101", "section_number": 1, "capacity": 2},
    )
    sec_id = sec_res.json()["id"]

    # 2. Reserve seat with KEY_A
    r1 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_A"})
    assert r1.status_code == 200
    assert r1.json()["reserved"] is True
    assert r1.json()["seat_key"] == "KEY_A"

    # Enrolled count should now be 1
    sec1 = (await client.get(f"/sections/{sec_id}")).json()
    assert sec1["enrolled_count"] == 1

    # 3. Idempotent reservation replay with KEY_A
    r1_dup = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_A"})
    assert r1_dup.status_code == 200
    assert r1_dup.json()["reserved"] is True

    # Count must remain 1
    sec1_dup = (await client.get(f"/sections/{sec_id}")).json()
    assert sec1_dup["enrolled_count"] == 1

    # 4. Release seat with KEY_A
    rel1 = await client.post(f"/sections/{sec_id}/seats/release", json={"seat_key": "KEY_A"})
    assert rel1.status_code == 200
    assert rel1.json()["released"] is True

    # Enrolled count returns to 0
    sec0 = (await client.get(f"/sections/{sec_id}")).json()
    assert sec0["enrolled_count"] == 0

    # 5. Idempotent release replay with KEY_A
    rel1_dup = await client.post(f"/sections/{sec_id}/seats/release", json={"seat_key": "KEY_A"})
    assert rel1_dup.status_code == 200
    assert rel1_dup.json()["released"] is True

    # Enrolled count stays at 0 (does not decrement below 0)
    sec0_dup = (await client.get(f"/sections/{sec_id}")).json()
    assert sec0_dup["enrolled_count"] == 0

    # 6. Cannot re-reserve with the released KEY_A
    re_res = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_A"})
    assert re_res.status_code == 409
    assert re_res.json()["reserved"] is False


@pytest.mark.asyncio
async def test_reserve_seat_capacity_full_and_expansion(api_client: AsyncClient):
    client = api_client

    # Section with capacity 1
    await client.post("/courses", json={"code": "CS201", "title": "Adv Algo", "credits": 3})
    sec = (
        await client.post(
            "/sections",
            json={"course_code": "CS201", "section_number": 1, "capacity": 1},
        )
    ).json()
    sec_id = sec["id"]

    # 1. Fill section
    r1 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_1"})
    assert r1.status_code == 200
    assert r1.json()["reserved"] is True

    # 2. Next reservation fails with 409 FULL
    r2 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_2"})
    assert r2.status_code == 409
    assert r2.json()["reserved"] is False
    assert r2.json()["reason"] == "FULL"

    # 3. Dynamically expand capacity to 2 (FR-14)
    patch_res = await client.patch(f"/sections/{sec_id}/capacity", json={"capacity": 2})
    assert patch_res.status_code == 200
    assert patch_res.json()["capacity"] == 2

    # 4. Now KEY_2 reservation succeeds!
    r2_retry = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_2"})
    assert r2_retry.status_code == 200
    assert r2_retry.json()["reserved"] is True

    sec_full = (await client.get(f"/sections/{sec_id}")).json()
    assert sec_full["enrolled_count"] == 2
    assert sec_full["capacity"] == 2


@pytest.mark.asyncio
async def test_seat_reservation_and_release_error_cases(api_client: AsyncClient):
    client = api_client

    # 1. 404 for non-existent section on reserve
    r_404 = await client.post("/sections/99999/seats/reserve", json={"seat_key": "KEY_X"})
    assert r_404.status_code == 404

    # 2. 404 for non-existent section on release
    rel_404 = await client.post("/sections/99999/seats/release", json={"seat_key": "KEY_X"})
    assert rel_404.status_code == 404

    # 3. Releasing unknown seat_key marks it cancelled and returns 200
    await client.post("/courses", json={"code": "CS301", "title": "SDA", "credits": 3})
    sec = (
        await client.post(
            "/sections",
            json={"course_code": "CS301", "section_number": 1, "capacity": 5},
        )
    ).json()
    sec_id = sec["id"]

    rel_unknown = await client.post(
        f"/sections/{sec_id}/seats/release", json={"seat_key": "KEY_UNKNOWN"}
    )
    assert rel_unknown.status_code == 200
    assert rel_unknown.json()["released"] is True

    # 4. Cross-section seat key reuse conflict
    sec2 = (
        await client.post(
            "/sections",
            json={"course_code": "CS301", "section_number": 2, "capacity": 5},
        )
    ).json()
    sec2_id = sec2["id"]

    await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_SHARED"})
    r_conflict = await client.post(
        f"/sections/{sec2_id}/seats/reserve", json={"seat_key": "KEY_SHARED"}
    )
    assert r_conflict.status_code == 409


@pytest.mark.asyncio
async def test_catalog_client_contract_integration(api_client: AsyncClient):
    """Directly verify that EnrollmentService's CatalogClient works with CatalogService."""
    client = api_client
    catalog_client = CatalogClient(client, "http://test")

    await client.post("/courses", json={"code": "CS101", "title": "Intro", "credits": 3})
    sec = (
        await client.post(
            "/sections",
            json={"course_code": "CS101", "section_number": 1, "capacity": 1},
        )
    ).json()
    sec_id = sec["id"]

    # 1. reserve_seat returns True
    res1 = await catalog_client.reserve_seat(sec_id, "TOKEN_1")
    assert res1 is True

    # 2. reserve_seat returns False when full
    res2 = await catalog_client.reserve_seat(sec_id, "TOKEN_2")
    assert res2 is False

    # 3. release_seat returns seat
    await catalog_client.release_seat(sec_id, "TOKEN_1")

    # 4. reserve_seat now succeeds
    res2_retry = await catalog_client.reserve_seat(sec_id, "TOKEN_2")
    assert res2_retry is True


@pytest.mark.asyncio
async def test_concurrent_reserve_seats_zero_overbooking(api_client: AsyncClient):
    """20 concurrent reservation requests against a section with capacity 5.

    Guarantees that exactly 5 succeed (200 OK) and 15 fail with 409 FULL,
    and enrolled_count terminates exactly at capacity 5.
    """
    client = api_client
    await client.post("/courses", json={"code": "CS101", "title": "Intro", "credits": 3})
    sec = (
        await client.post(
            "/sections",
            json={"course_code": "CS101", "section_number": 1, "capacity": 5},
        )
    ).json()
    sec_id = sec["id"]

    tasks = [
        client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": f"BURST_{i}"})
        for i in range(20)
    ]
    responses = await asyncio.gather(*tasks)

    succeeded = [r for r in responses if r.status_code == 200]
    full = [r for r in responses if r.status_code == 409]

    assert len(succeeded) == 5
    assert len(full) == 15

    final_sec = (await client.get(f"/sections/{sec_id}")).json()
    assert final_sec["enrolled_count"] == 5


@pytest.mark.asyncio
async def test_saturation_cache_fast_fail_and_eviction(api_client: AsyncClient):
    """Verify that in-memory saturation cache fast-fails without database updates (ADR-003)."""
    from catalog_service.main import _saturation_cache, clear_saturation_cache

    clear_saturation_cache()
    client = api_client

    await client.post("/courses", json={"code": "CS105", "title": "Cache Test", "credits": 3})
    sec = (
        await client.post(
            "/sections",
            json={"course_code": "CS105", "section_number": 1, "capacity": 1},
        )
    ).json()
    sec_id = sec["id"]

    # 1. Fill capacity
    r1 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "SAT_1"})
    assert r1.status_code == 200

    # 2. Section is now full; first failure arms saturation cache
    r2 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "SAT_2"})
    assert r2.status_code == 409
    assert sec_id in _saturation_cache

    # 3. Subsequent request hits saturation cache fast-path
    r3 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "SAT_3"})
    assert r3.status_code == 409
    assert r3.json()["reason"] == "FULL"

    # 4. Release seat evicts from saturation cache
    rel = await client.post(f"/sections/{sec_id}/seats/release", json={"seat_key": "SAT_1"})
    assert rel.status_code == 200
    assert sec_id not in _saturation_cache

    # 5. Now reservation succeeds again
    r4 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "SAT_4"})
    assert r4.status_code == 200


@pytest.mark.asyncio
async def test_saturation_cache_interleaved_release_does_not_arm_cache(tmp_path):
    """Verify that an interleaved seat release during reservation failure prevents arming the cache."""
    from catalog_service.main import (
        _saturation_cache,
        clear_saturation_cache,
        evict_section_saturation,
    )

    clear_saturation_cache()
    test_db_path = tmp_path / "test_interleave.db"
    test_engine = make_engine(f"sqlite+aiosqlite:///{test_db_path.as_posix()}")
    await init_db(test_engine)

    test_session_maker = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    target_sec_id = None

    async def override_get_db():
        async with test_session_maker() as session:
            orig_get = session.get

            async def hooked_get(entity, ident, **kwargs):
                if target_sec_id is not None and ident == target_sec_id:
                    # Simulate a concurrent release committing while Request A is yielded on session.get
                    evict_section_saturation(target_sec_id)
                return await orig_get(entity, ident, **kwargs)

            session.get = hooked_get
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/courses", json={"code": "CS106", "title": "Race Test", "credits": 3})
        sec = (
            await client.post(
                "/sections",
                json={"course_code": "CS106", "section_number": 1, "capacity": 1},
            )
        ).json()
        sec_id = sec["id"]

        # 1. Fill capacity (1/1)
        r1 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "OCC_1"})
        assert r1.status_code == 200

        # Enable interception hook for section sec_id
        target_sec_id = sec_id

        # 2. Second request fails (0 rows updated), but while awaiting session.get,
        # hooked_get triggers an eviction/version bump
        r2 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "FAIL_1"})
        assert r2.status_code == 409
        assert r2.json()["reason"] == "FULL"

        # 3. Assert that because of the interleaved release, the section was NOT cached in _saturation_cache!
        assert sec_id not in _saturation_cache

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_released_or_cancelled_seat_key_returns_proper_reason_when_section_saturated(
    api_client: AsyncClient,
):
    """
    Verify that replaying a RELEASED or CANCELLED seat key on a saturated section
    returns HTTP 409 with reason 'RELEASED' or 'CANCELLED', not 'FULL'.
    """
    from catalog_service.main import _saturation_cache, clear_saturation_cache

    client = api_client
    clear_saturation_cache()

    # 1. Setup course and 1-seat section
    await client.post("/courses", json={"code": "CS107", "title": "Status Test", "credits": 3})
    sec_res = await client.post(
        "/sections",
        json={"course_code": "CS107", "section_number": 1, "capacity": 1},
    )
    sec_id = sec_res.json()["id"]

    # 2. Reserve seat with KEY_REL, then release it
    r1 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_REL"})
    assert r1.status_code == 200
    rel = await client.post(f"/sections/{sec_id}/seats/release", json={"seat_key": "KEY_REL"})
    assert rel.status_code == 200

    # 3. Fill the section with KEY_ACTIVE (1/1 full)
    r2 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_ACTIVE"})
    assert r2.status_code == 200

    # 4. Saturated attempt with fresh key arms the saturation cache
    r3 = await client.post(f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_BLOCKED"})
    assert r3.status_code == 409
    assert r3.json()["reason"] == "FULL"
    assert sec_id in _saturation_cache

    # 5. Idempotent replay of KEY_ACTIVE on saturated section returns 200 OK
    r_active_replay = await client.post(
        f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_ACTIVE"}
    )
    assert r_active_replay.status_code == 200
    assert r_active_replay.json()["reserved"] is True

    # 6. Replaying KEY_REL on the saturated section MUST return reason 'RELEASED', not 'FULL'
    r_rel_replay = await client.post(
        f"/sections/{sec_id}/seats/reserve", json={"seat_key": "KEY_REL"}
    )
    assert r_rel_replay.status_code == 409
    assert r_rel_replay.json()["reserved"] is False
    assert r_rel_replay.json()["reason"] == "RELEASED"
    assert "released" in r_rel_replay.json()["detail"].lower()




