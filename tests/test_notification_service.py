"""
tests/test_notification_service.py
Automated test suite verifying the Notification Service and GoF Observer Pattern.
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from common.auth import create_access_token
from common.events import (
    CourseDroppedEvent,
    EnrollmentConfirmedEvent,
    WaitlistJoinedEvent,
    WaitlistSlotOfferedEvent,
)
from notification_service.database import (
    get_db,
    init_db,
    make_engine,
    make_session_maker,
)
from notification_service.event_consumer import (
    CourseDroppedObserver,
    EnrollmentConfirmedObserver,
    EventDispatcher,
    EventObserver,
    WaitlistJoinedObserver,
    WaitlistSlotOfferedObserver,
    create_default_dispatcher,
)
from notification_service.main import app as notif_app
from notification_service.main import get_dispatcher
from notification_service.models import NotificationLog
from tests.gateway_support import (
    NOTIFICATION_PORT,
    GatewayTestEnv,
)


@pytest.fixture
async def notif_db(tmp_path):
    """Temporary isolated SQLite database for notification service testing."""
    db_file = tmp_path / "test_notifications.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_db(engine)
    session_maker = make_session_maker(engine)
    yield session_maker
    await engine.dispose()


@pytest.fixture
async def notif_client(notif_db):
    """Async test client bound to the notification app with an isolated database."""
    dispatcher = create_default_dispatcher(notif_db)

    async def override_get_db():
        async with notif_db() as session:
            yield session

    notif_app.dependency_overrides[get_db] = override_get_db
    notif_app.dependency_overrides[get_dispatcher] = lambda: dispatcher

    async with AsyncClient(
        transport=ASGITransport(app=notif_app), base_url="http://test"
    ) as client:
        yield client

    notif_app.dependency_overrides.clear()


# =====================================================================
# Unit Tests: GoF Observer Pattern Components
# =====================================================================


async def test_enrollment_confirmed_observer(notif_db):
    observer = EnrollmentConfirmedObserver()
    event = EnrollmentConfirmedEvent(
        event_id=str(uuid4()),
        student_id="STU_100",
        section_id=101,
        course_code="CS301",
        credits=3,
    )

    success = await observer.handle(event.model_dump(mode="json"), notif_db)
    assert success is True

    async with notif_db() as session:
        result = await session.execute(
            select(NotificationLog).where(NotificationLog.recipient_id == "STU_100")
        )
        logs = result.scalars().all()

    assert len(logs) == 1
    assert logs[0].event_type == "ENROLLMENT_CONFIRMED"
    assert "CS301" in logs[0].subject
    assert "Section 101" in logs[0].message
    assert "3 credits" in logs[0].message


async def test_waitlist_joined_observer(notif_db):
    observer = WaitlistJoinedObserver()
    event = WaitlistJoinedEvent(
        event_id=str(uuid4()),
        student_id="STU_101",
        section_id=102,
        course_code="CS302",
        position=4,
    )

    success = await observer.handle(event.model_dump(mode="json"), notif_db)
    assert success is True

    async with notif_db() as session:
        result = await session.execute(
            select(NotificationLog).where(NotificationLog.recipient_id == "STU_101")
        )
        log = result.scalar_one()

    assert log.event_type == "WAITLIST_JOINED"
    assert "queue position #4" in log.message


async def test_course_dropped_observer(notif_db):
    observer = CourseDroppedObserver()
    event = CourseDroppedEvent(
        event_id=str(uuid4()),
        student_id="STU_102",
        section_id=103,
        course_code="CS201",
    )

    success = await observer.handle(event.model_dump(mode="json"), notif_db)
    assert success is True

    async with notif_db() as session:
        result = await session.execute(
            select(NotificationLog).where(NotificationLog.recipient_id == "STU_102")
        )
        log = result.scalar_one()

    assert log.event_type == "COURSE_DROPPED"
    assert "released" in log.message


async def test_waitlist_slot_offered_observer(notif_db):
    observer = WaitlistSlotOfferedObserver()
    event = WaitlistSlotOfferedEvent(
        event_id=str(uuid4()),
        offer_id=50,
        student_id="STU_103",
        section_id=104,
        course_code="CS202",
        expires_at=datetime.now(UTC),
    )

    success = await observer.handle(event.model_dump(mode="json"), notif_db)
    assert success is True

    async with notif_db() as session:
        result = await session.execute(
            select(NotificationLog).where(NotificationLog.recipient_id == "STU_103")
        )
        log = result.scalar_one()

    assert log.event_type == "WAITLIST_SLOT_OFFERED"
    assert "claim your seat" in log.message


async def test_dispatcher_handles_unknown_events_gracefully(notif_db):
    dispatcher = create_default_dispatcher(notif_db)
    count = await dispatcher.dispatch({"event_type": "UNKNOWN_CUSTOM_TOPIC"})
    assert count == 0


async def test_dispatcher_multiple_observers_same_topic(notif_db):
    dispatcher = EventDispatcher(notif_db)

    class CustomObserver(EventObserver):
        name = "CustomObserver"
        called = False

        async def handle(self, event_data, session_maker):
            self.called = True
            return True

    custom = CustomObserver()
    dispatcher.register("ENROLLMENT_CONFIRMED", EnrollmentConfirmedObserver())
    dispatcher.register("ENROLLMENT_CONFIRMED", custom)

    count = await dispatcher.dispatch(
        {
            "event_type": "ENROLLMENT_CONFIRMED",
            "student_id": "STU_999",
            "course_code": "CS999",
            "section_id": 999,
            "credits": 3,
        }
    )
    assert count == 2
    assert custom.called is True


# =====================================================================
# API Integration Tests: Notification Service Endpoints
# =====================================================================


async def test_health_and_root(notif_client: AsyncClient):
    res = await notif_client.get("/health")
    assert res.status_code == 200
    assert res.json()["service"] == "notification_service"
    assert res.json()["status"] == "healthy"

    root_res = await notif_client.get("/")
    assert root_res.status_code == 200


async def test_post_event_accepted_and_stores_notification(notif_client: AsyncClient):
    payload = {
        "event_id": str(uuid4()),
        "event_type": "ENROLLMENT_CONFIRMED",
        "student_id": "STU_042",
        "section_id": 101,
        "course_code": "CS301",
        "credits": 3,
    }

    res = await notif_client.post("/events", json=payload)
    assert res.status_code == 202
    data = res.json()
    assert data["accepted"] is True
    assert data["handlers_notified"] == 1

    # Query notifications for STU_042
    get_res = await notif_client.get("/notifications/STU_042")
    assert get_res.status_code == 200
    alerts = get_res.json()
    assert len(alerts) == 1
    assert alerts[0]["recipient_id"] == "STU_042"
    assert alerts[0]["event_type"] == "ENROLLMENT_CONFIRMED"
    assert "Registration Confirmed" in alerts[0]["subject"]


async def test_post_event_missing_type_returns_400(notif_client: AsyncClient):
    res = await notif_client.post("/events", json={"student_id": "STU_001"})
    assert res.status_code == 400


async def test_empty_notification_zero_state(notif_client: AsyncClient):
    res = await notif_client.get("/notifications/NONEXISTENT_STUDENT")
    assert res.status_code == 200
    assert res.json() == []


async def test_student_notifications_chronological_ordering(notif_client: AsyncClient):
    student_id = "STU_MULTI"
    events = [
        {"event_type": "ENROLLMENT_CONFIRMED", "student_id": student_id, "course_code": "CS101", "section_id": 1},
        {"event_type": "WAITLIST_JOINED", "student_id": student_id, "course_code": "CS201", "section_id": 2, "position": 1},
        {"event_type": "COURSE_DROPPED", "student_id": student_id, "course_code": "CS101", "section_id": 1},
    ]

    for ev in events:
        ev["event_id"] = str(uuid4())
        post_res = await notif_client.post("/events", json=ev)
        assert post_res.status_code == 202

    res = await notif_client.get(f"/notifications/{student_id}")
    assert res.status_code == 200
    alerts = res.json()
    assert len(alerts) == 3
    # Newest first
    assert alerts[0]["event_type"] == "COURSE_DROPPED"
    assert alerts[1]["event_type"] == "WAITLIST_JOINED"
    assert alerts[2]["event_type"] == "ENROLLMENT_CONFIRMED"


async def test_notifications_isolated_between_students(notif_client: AsyncClient):
    await notif_client.post(
        "/events",
        json={"event_id": "e1", "event_type": "ENROLLMENT_CONFIRMED", "student_id": "STU_A", "course_code": "CS101", "section_id": 1},
    )
    await notif_client.post(
        "/events",
        json={"event_id": "e2", "event_type": "ENROLLMENT_CONFIRMED", "student_id": "STU_B", "course_code": "CS102", "section_id": 1},
    )

    res_a = await notif_client.get("/notifications/STU_A")
    assert len(res_a.json()) == 1
    assert res_a.json()[0]["recipient_id"] == "STU_A"

    res_b = await notif_client.get("/notifications/STU_B")
    assert len(res_b.json()) == 1
    assert res_b.json()[0]["recipient_id"] == "STU_B"


async def test_reset_notifications_endpoint(notif_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_RESET", "true")
    await notif_client.post(
        "/events",
        json={"event_id": "e1", "event_type": "ENROLLMENT_CONFIRMED", "student_id": "STU_RESET", "course_code": "CS101", "section_id": 1},
    )
    res_before = await notif_client.get("/notifications/STU_RESET")
    assert len(res_before.json()) == 1

    reset_res = await notif_client.post("/reset")
    assert reset_res.status_code == 200

    res_after = await notif_client.get("/notifications/STU_RESET")
    assert len(res_after.json()) == 0


# =====================================================================
# API Gateway Reverse Proxy & Protection Tests
# =====================================================================


async def test_gateway_notifications_requires_auth(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.get("/api/v1/notifications")
    assert res.status_code == 401


async def test_gateway_proxies_notifications_with_auth(gateway_env: GatewayTestEnv):
    token = create_access_token("STU_001")
    gateway_env.downstreams.respond(
        "GET",
        NOTIFICATION_PORT,
        "/notifications/STU_001",
        200,
        [
            {
                "id": 1,
                "event_id": "ev-1",
                "recipient_id": "STU_001",
                "event_type": "ENROLLMENT_CONFIRMED",
                "subject": "Registration Confirmed",
                "message": "Enrolled in CS301",
                "channel": "IN_APP",
                "status": "DELIVERED",
                "created_at": "2026-10-15T12:00:00Z",
            }
        ],
    )

    res = await gateway_env.client.get(
        "/api/v1/notifications",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["recipient_id"] == "STU_001"

    # Verify header propagation
    req = gateway_env.downstreams.last
    assert req.headers["x-student-id"] == "STU_001"
