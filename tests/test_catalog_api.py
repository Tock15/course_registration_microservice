"""
tests/test_catalog_api.py
Integration tests verifying Course Catalog Service REST API endpoints,
search filters, capacity telemetry, and prerequisite validation.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalog_service.database import get_db
from catalog_service.main import app
from catalog_service.models import Base


@pytest.fixture
async def api_client():
    """Isolated in-memory test client with database dependency override."""
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_maker = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

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
            "student_id": "STU_002",
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
