"""
tests/test_student_api.py
Integration tests verifying Student Profile Service REST API endpoints,
eligibility evaluations, and credential verification.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from student_service.database import get_db
from student_service.main import app
from student_service.models import Base
from student_service.seed import seed_students


@pytest.fixture
async def api_client():
    """Isolated in-memory test client with database dependency override and seed data."""
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_maker = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Seed the in-memory database
    async with test_session_maker() as session:
        await seed_students(session)

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
async def test_student_list_and_filters(api_client: AsyncClient):
    """Verify listing students and applying standing, hold, and search filters."""
    client = api_client

    # 1. List all students (7 seeded)
    res = await client.get("/students")
    assert res.status_code == 200
    all_students = res.json()
    assert len(all_students) == 7

    # 2. Filter by academic standing PROBATION
    res_prob = await client.get("/students?standing=PROBATION")
    assert res_prob.status_code == 200
    prob_list = res_prob.json()
    assert len(prob_list) == 1
    assert prob_list[0]["id"] == "STU_004"

    # 3. Filter by financial hold
    res_hold = await client.get("/students?has_hold=true")
    assert res_hold.status_code == 200
    hold_list = res_hold.json()
    assert len(hold_list) == 1
    assert hold_list[0]["id"] == "STU_005"

    # 4. Search keyword
    res_search = await client.get("/students?search=Alice")
    assert res_search.status_code == 200
    assert len(res_search.json()) == 1
    assert res_search.json()[0]["id"] == "STU_001"


@pytest.mark.asyncio
async def test_get_student_detail(api_client: AsyncClient):
    """Verify retrieving student profile details and transcript courses."""
    client = api_client

    res = await client.get("/students/STU_001")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "STU_001"
    assert data["name"] == "Alice Smith"
    assert data["gpa"] == 3.85
    assert data["academic_standing"] == "GOOD_STANDING"
    assert data["financial_hold"] is False
    assert "CS101" in data["completed_courses"]
    assert "CS102" in data["completed_courses"]

    # 404 on missing student
    res_404 = await client.get("/students/STU_NONEXISTENT")
    assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_eligibility_scenarios(api_client: AsyncClient):
    """Verify eligibility logic for Good Standing, Holds, Suspension, and Probation."""
    client = api_client

    # Case 1: STU_001 (Good standing, 0 enrolled credits, max 22) -> Eligible
    res_1 = await client.get("/students/STU_001/eligibility")
    assert res_1.status_code == 200
    elig_1 = res_1.json()
    assert elig_1["is_eligible"] is True
    assert elig_1["reason"] is None
    assert elig_1["remaining_credits"] == 22

    # Case 2: STU_005 (Financial hold active) -> Ineligible
    res_5 = await client.get("/students/STU_005/eligibility")
    assert res_5.status_code == 200
    elig_5 = res_5.json()
    assert elig_5["is_eligible"] is False
    assert "Financial hold" in elig_5["reason"]
    assert elig_5["remaining_credits"] == 0

    # Case 3: STU_006 (Academic suspension) -> Ineligible
    res_6 = await client.get("/students/STU_006/eligibility")
    assert res_6.status_code == 200
    elig_6 = res_6.json()
    assert elig_6["is_eligible"] is False
    assert "suspension" in elig_6["reason"]

    # Case 4: STU_004 (Academic probation, max 15 credits) -> Eligible with remaining=15
    res_4 = await client.get("/students/STU_004/eligibility")
    assert res_4.status_code == 200
    elig_4 = res_4.json()
    assert elig_4["is_eligible"] is True
    assert elig_4["remaining_credits"] == 15

    # Case 5: STU_007 (enrolled_credits == 22, max == 22) -> Ineligible (credit limit reached)
    res_7 = await client.get("/students/STU_007/eligibility")
    assert res_7.status_code == 200
    elig_7 = res_7.json()
    assert elig_7["is_eligible"] is False
    assert "credit limit" in elig_7["reason"]


@pytest.mark.asyncio
async def test_credential_verification_endpoint(api_client: AsyncClient):
    """Verify student password check for API Gateway."""
    client = api_client

    # 1. Valid credentials
    res_valid = await client.post(
        "/students/verify-credentials",
        json={"student_id": "STU_001", "password": "password123"},
    )
    assert res_valid.status_code == 200
    data_valid = res_valid.json()
    assert data_valid["valid"] is True
    assert data_valid["student_id"] == "STU_001"
    assert data_valid["role"] == "STUDENT"
    assert data_valid["name"] == "Alice Smith"

    # 2. Invalid password
    res_wrong = await client.post(
        "/students/verify-credentials",
        json={"student_id": "STU_001", "password": "incorrect_password"},
    )
    assert res_wrong.status_code == 200
    data_wrong = res_wrong.json()
    assert data_wrong["valid"] is False

    # 3. Nonexistent user
    res_none = await client.post(
        "/students/verify-credentials",
        json={"student_id": "STU_NONEXISTENT", "password": "any"},
    )
    assert res_none.status_code == 200
    assert res_none.json()["valid"] is False


@pytest.mark.asyncio
async def test_student_management_and_patching(api_client: AsyncClient):
    """Verify student registration, toggling holds, and updating credits."""
    client = api_client

    # 1. Create a new student
    res_create = await client.post(
        "/students",
        json={
            "id": "STU_088",
            "name": "Bruce Wayne",
            "email": "bruce.wayne@university.edu",
            "password": "batmanpassword",
            "major": "Computer Engineering",
            "year": 1,
            "gpa": 3.90,
            "academic_standing": "GOOD_STANDING",
            "financial_hold": False,
            "completed_courses": [],
            "enrolled_credits": 0,
            "max_credits": 22,
        },
    )
    assert res_create.status_code == 201
    assert res_create.json()["id"] == "STU_088"

    # 2. Toggle financial hold
    res_hold = await client.patch(
        "/students/STU_088/hold",
        json={"financial_hold": True},
    )
    assert res_hold.status_code == 200
    assert res_hold.json()["financial_hold"] is True

    # Check eligibility now fails due to hold
    res_elig = await client.get("/students/STU_088/eligibility")
    assert res_elig.json()["is_eligible"] is False

    # 3. Release hold
    await client.patch(
        "/students/STU_088/hold",
        json={"financial_hold": False},
    )

    # 4. Update enrolled credits
    res_cred = await client.patch(
        "/students/STU_088/credits",
        json={"enrolled_credits": 9},
    )
    assert res_cred.status_code == 200
    assert res_cred.json()["enrolled_credits"] == 9

    # Check eligibility reflects 13 remaining credits (22 - 9)
    res_elig_after = await client.get("/students/STU_088/eligibility")
    assert res_elig_after.json()["is_eligible"] is True
    assert res_elig_after.json()["remaining_credits"] == 13

    # 5. Update completed courses
    res_cc = await client.patch(
        "/students/STU_088/completed-courses",
        json={"completed_courses": ["CS101", "MATH101"]},
    )
    assert res_cc.status_code == 200
    assert res_cc.json()["completed_courses"] == ["CS101", "MATH101"]
