"""
enrollment_service/tests/conftest.py
Shared fixtures: an isolated file-based enrollment.db, mocked upstream
services served through httpx.MockTransport (so the real HTTP clients run),
and an ASGI test client with dependency overrides.

FakeUpstreams' Catalog implements the proposed seat contract:
  POST /sections/{id}/seats/reserve {"seat_key"}  -> 200 reserved | 409 FULL
  POST /sections/{id}/seats/release {"seat_key"}  -> 200, idempotent
"""
import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.events import BaseEvent
from enrollment_service.clients import CatalogClient, StudentClient
from enrollment_service.database import (
    get_session_maker,
    init_db,
    make_engine,
    make_session_maker,
)
from enrollment_service.events import EventPublisher
from enrollment_service.main import (
    app,
    get_catalog_client,
    get_event_publisher,
    get_student_client,
)
from enrollment_service.models import Enrollment

STUDENT_HOST = "student.test"
CATALOG_HOST = "catalog.test"


class FakeUpstreams:
    """In-memory Student + Catalog services, including Catalog's seat contract."""

    def __init__(self) -> None:
        self.students: dict[str, dict] = {}
        self.courses: dict[str, dict] = {}
        self.sections: dict[int, dict] = {}
        self.reservations: dict[str, str] = {}
        self.seat_calls: list[tuple[str, int, str]] = []
        self.student_down = False
        self.catalog_down = False
        self.reserve_down = False
        self._seat_lock = asyncio.Lock()

    def add_course(self, code: str, credits: int = 3, prereqs: tuple[str, ...] = ()) -> None:
        self.courses[code] = {
            "code": code,
            "title": f"Course {code}",
            "credits": credits,
            "prerequisites": list(prereqs),
        }

    def add_section(
        self,
        section_id: int,
        course_code: str,
        *,
        capacity: int = 40,
        enrolled: int = 0,
        days: tuple[str, ...] = ("MON", "WED"),
        start: str = "09:00",
        end: str = "10:30",
        number: int = 1,
    ) -> None:
        self.sections[section_id] = {
            "id": section_id,
            "course_code": course_code,
            "section_number": number,
            "capacity": capacity,
            "enrolled_count": enrolled,
            "schedule_days": list(days),
            "start_time": start,
            "end_time": end,
            "room": "HM402",
            "instructor": "Dr. Test",
        }

    def add_student(
        self,
        student_id: str,
        *,
        completed: tuple[str, ...] = (),
        hold: bool = False,
        standing: str = "GOOD_STANDING",
        max_credits: int = 22,
    ) -> None:
        self.students[student_id] = {
            "id": student_id,
            "name": f"Student {student_id}",
            "gpa": 3.0,
            "academic_standing": standing,
            "financial_hold": hold,
            "completed_courses": list(completed),
            "enrolled_credits": 0,
            "max_credits": max_credits,
        }

    def seats_held(self, section_id: int) -> int:
        return self.sections[section_id]["enrolled_count"]

    def set_capacity(self, section_id: int, capacity: int) -> None:
        self.sections[section_id]["capacity"] = capacity

    async def handler(self, request: httpx.Request) -> httpx.Response:
        parts = [p for p in request.url.path.split("/") if p]
        if request.url.host == STUDENT_HOST:
            if self.student_down:
                raise httpx.ConnectError("student service down", request=request)
            return self._student(request.method, parts)
        if request.url.host == CATALOG_HOST:
            if self.catalog_down:
                raise httpx.ConnectError("catalog service down", request=request)
            return await self._catalog(request, parts)
        return httpx.Response(404)

    def _student(self, method: str, parts: list[str]) -> httpx.Response:
        if method != "GET" or len(parts) < 2 or parts[0] != "students":
            return httpx.Response(404)
        student = self.students.get(parts[1])
        if student is None:
            return httpx.Response(404, json={"detail": "Student not found"})
        if len(parts) == 2:
            return httpx.Response(200, json=student)
        reason = None
        if student["financial_hold"]:
            reason = "Financial hold on account."
        elif student["academic_standing"] == "SUSPENDED":
            reason = "Academic suspension."
        return httpx.Response(
            200,
            json={
                "student_id": student["id"],
                "is_eligible": reason is None,
                "reason": reason,
                "remaining_credits": student["max_credits"],
            },
        )

    async def _catalog(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        method = request.method
        if method == "GET" and parts[:1] == ["sections"] and len(parts) == 2:
            section = self.sections.get(int(parts[1]))
            if section is None:
                return httpx.Response(404, json={"detail": "Section not found"})
            return httpx.Response(200, json=section)

        if method == "GET" and parts[:1] == ["courses"] and len(parts) == 2:
            course = self.courses.get(parts[1])
            if course is None:
                return httpx.Response(404, json={"detail": "Course not found"})
            return httpx.Response(200, json=course)

        if method == "POST" and parts == ["validate-prereqs"]:
            body = json.loads(request.content)
            course = self.courses.get(body["course_code"])
            if course is None:
                return httpx.Response(
                    200, json={"is_valid": False, "missing_prereqs": [], "message": "No course"}
                )
            done = set(body.get("completed_courses", []))
            missing = sorted(set(course["prerequisites"]) - done)
            return httpx.Response(
                200,
                json={
                    "is_valid": not missing,
                    "missing_prereqs": missing,
                    "message": (
                        f"Missing required prerequisite(s): {', '.join(missing)}"
                        if missing
                        else "All prerequisites satisfied."
                    ),
                },
            )

        if method == "POST" and len(parts) == 4 and parts[0] == "sections" and parts[2] == "seats":
            section_id, action = int(parts[1]), parts[3]
            seat_key = json.loads(request.content)["seat_key"]
            if action == "reserve" and self.reserve_down:
                raise httpx.ConnectError("reserve endpoint down", request=request)
            await asyncio.sleep(0)
            async with self._seat_lock:
                self.seat_calls.append((action, section_id, seat_key))
                section = self.sections.get(section_id)
                if section is None:
                    return httpx.Response(404, json={"detail": "Section not found"})
                if action == "reserve":
                    return self._reserve(section, seat_key)
                if action == "release":
                    return self._release(section, seat_key)

        return httpx.Response(404)

    def _reserve(self, section: dict, seat_key: str) -> httpx.Response:
        state = self.reservations.get(seat_key)
        if state == "ACTIVE":
            return httpx.Response(200, json={"reserved": True})
        if state is not None:
            return httpx.Response(409, json={"reserved": False, "reason": state})
        if section["enrolled_count"] < section["capacity"]:
            section["enrolled_count"] += 1
            self.reservations[seat_key] = "ACTIVE"
            return httpx.Response(200, json={"reserved": True})
        return httpx.Response(409, json={"reserved": False, "reason": "FULL"})

    def _release(self, section: dict, seat_key: str) -> httpx.Response:
        state = self.reservations.get(seat_key)
        if state == "ACTIVE":
            section["enrolled_count"] -= 1
            self.reservations[seat_key] = "RELEASED"
        elif state is None:
            self.reservations[seat_key] = "CANCELLED"
        return httpx.Response(200, json={"released": True})


class RecordingPublisher(EventPublisher):
    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    def publish(self, event: BaseEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]


@dataclass
class EnrollmentTestEnv:
    client: AsyncClient
    upstreams: FakeUpstreams
    sessions: async_sessionmaker[AsyncSession]
    catalog: CatalogClient
    publisher: RecordingPublisher

    async def enroll(self, student_id: str, section_id: int) -> httpx.Response:
        return await self.client.post(
            "/enrollments",
            json={"section_id": section_id},
            headers={"X-Student-Id": student_id},
        )

    async def drop(self, student_id: str, section_id: int) -> httpx.Response:
        return await self.client.post(
            "/drops",
            json={"section_id": section_id},
            headers={"X-Student-Id": student_id},
        )

    async def mine(self, student_id: str) -> dict:
        res = await self.client.get("/enrollments", headers={"X-Student-Id": student_id})
        assert res.status_code == 200
        return res.json()

    async def rows(self, section_id: int | None = None) -> list[Enrollment]:
        async with self.sessions() as session, session.begin():
            stmt = select(Enrollment).order_by(Enrollment.id)
            if section_id is not None:
                stmt = stmt.where(Enrollment.section_id == section_id)
            return list((await session.execute(stmt)).scalars().all())

    async def status_of(self, student_id: str, section_id: int) -> str | None:
        rows = [r for r in await self.rows(section_id) if r.student_id == student_id]
        return rows[-1].status if rows else None


@pytest.fixture
def upstreams() -> FakeUpstreams:
    """Mini curriculum mirroring catalog_service/seed.py."""
    fake = FakeUpstreams()
    fake.add_course("CS101")
    fake.add_course("CS102", prereqs=("CS101",))
    fake.add_course("CS202", prereqs=("CS101",))
    fake.add_course("CS301", prereqs=("CS102", "CS202"))
    fake.add_course("CS303", prereqs=("CS202",))

    fake.add_section(101, "CS301", capacity=10, days=("MON", "WED"), start="09:00", end="10:30")
    fake.add_section(
        102, "CS301", capacity=50, days=("MON",), start="13:00", end="16:00", number=2
    )
    fake.add_section(103, "CS101", capacity=60, days=("TUE", "THU"), start="09:00", end="10:30")
    fake.add_section(104, "CS102", capacity=45, days=("MON", "WED"), start="13:00", end="14:30")
    fake.add_section(105, "CS202", capacity=1, days=("FRI",), start="09:00", end="12:00")
    fake.add_section(106, "CS303", capacity=40, days=("WED",), start="09:00", end="12:00")

    senior = ("CS101", "CS102", "CS202")
    for i in range(1, 6):
        fake.add_student(f"STU_00{i}", completed=senior)
    fake.add_student("STU_NEW")
    fake.add_student("STU_HOLD", completed=senior, hold=True)
    return fake


@pytest.fixture
async def env(tmp_path, upstreams: FakeUpstreams):
    """Enrollment app wired to a temp file DB and the fake upstream services."""
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'enrollment.db').as_posix()}")
    await init_db(engine)
    sessions = make_session_maker(engine)

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handler))
    student = StudentClient(http, f"http://{STUDENT_HOST}")
    catalog = CatalogClient(http, f"http://{CATALOG_HOST}")
    publisher = RecordingPublisher()

    app.dependency_overrides[get_session_maker] = lambda: sessions
    app.dependency_overrides[get_student_client] = lambda: student
    app.dependency_overrides[get_catalog_client] = lambda: catalog
    app.dependency_overrides[get_event_publisher] = lambda: publisher

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield EnrollmentTestEnv(
            client=client,
            upstreams=upstreams,
            sessions=sessions,
            catalog=catalog,
            publisher=publisher,
        )

    app.dependency_overrides.clear()
    await http.aclose()
    await engine.dispose()
