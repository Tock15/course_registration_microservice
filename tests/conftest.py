"""Shared fixtures; enrollment fixtures wire the app to a temp DB and fake upstreams."""
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from enrollment_service.clients import CatalogClient, StudentClient
from enrollment_service.database import (
    get_session_maker,
    init_db,
    make_engine,
    make_session_maker,
)
from enrollment_service.main import (
    app,
    get_catalog_client,
    get_event_publisher,
    get_student_client,
)
from tests.enrollment_support import (
    CATALOG_HOST,
    STUDENT_HOST,
    EnrollmentTestEnv,
    FakeUpstreams,
    RecordingPublisher,
)


@pytest.fixture
def enrollment_upstreams() -> FakeUpstreams:
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
async def enrollment_env(tmp_path, enrollment_upstreams: FakeUpstreams):
    """Enrollment app wired to a temp file DB and the fake upstream services."""
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'enrollment.db').as_posix()}")
    await init_db(engine)
    sessions = make_session_maker(engine)

    http = httpx.AsyncClient(transport=httpx.MockTransport(enrollment_upstreams.handler))
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
            upstreams=enrollment_upstreams,
            sessions=sessions,
            catalog=catalog,
            publisher=publisher,
        )

    app.dependency_overrides.clear()
    await http.aclose()
    await engine.dispose()
