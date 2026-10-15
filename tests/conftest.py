"""Shared fixtures; enrollment fixtures wire the app to PostgreSQL (if running) or fallback SQLite."""
import asyncio
import os

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api_gateway.main import app as gateway_app
from api_gateway.main import wire_gateway
from api_gateway.rate_limiter import TokenBucketRateLimiter
from enrollment_service.clients import CatalogClient, StudentClient
from enrollment_service.config import settings
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
from enrollment_service.models import Base
from tests.enrollment_support import (
    CATALOG_HOST,
    STUDENT_HOST,
    EnrollmentTestEnv,
    FakeUpstreams,
    RecordingPublisher,
)
from tests.gateway_support import FakeClock, FakeDownstreams, GatewayTestEnv

_PG_AVAILABLE: bool | None = None
_PG_URL: str | None = None


async def _check_postgres() -> tuple[bool, str | None]:
    """Detect whether a live PostgreSQL instance is accessible for integration tests."""
    global _PG_AVAILABLE, _PG_URL
    if _PG_AVAILABLE is not None:
        return _PG_AVAILABLE, _PG_URL

    candidate_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not candidate_url and settings.database_url and "postgres" in settings.database_url:
        candidate_url = settings.database_url

    if candidate_url and "postgres" in candidate_url:
        probe_engine = make_engine(candidate_url)
        try:
            async with asyncio.timeout(0.5):
                async with probe_engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            _PG_AVAILABLE = True
            _PG_URL = candidate_url
            return True, candidate_url
        except (OSError, TimeoutError, SQLAlchemyError):
            _PG_AVAILABLE = False
            _PG_URL = None
            return False, None
        finally:
            await probe_engine.dispose()

    _PG_AVAILABLE = False
    _PG_URL = None
    return False, None


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
    """Enrollment app wired to PostgreSQL (if detected/running) or fallback to SQLite."""
    is_pg, pg_url = await _check_postgres()
    if is_pg and pg_url:
        engine = make_engine(pg_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
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
    if is_pg:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def in_memory_enrollment_sessions():
    """In-memory SQLite session factory for fast enrollment unit testing."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    sessions = make_session_maker(engine)
    yield sessions
    await engine.dispose()


GATEWAY_RATE_LIMIT_CAPACITY = 5


@pytest.fixture
async def gateway_env():
    """Gateway app wired to fake downstream services and a manual rate-limit clock."""
    downstreams = FakeDownstreams()
    clock = FakeClock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(downstreams.handler))
    wire_gateway(
        gateway_app,
        http,
        limiter=TokenBucketRateLimiter(GATEWAY_RATE_LIMIT_CAPACITY, 1.0, clock=clock),
        login_limiter=TokenBucketRateLimiter(GATEWAY_RATE_LIMIT_CAPACITY, 1.0, clock=clock),
    )

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app), base_url="http://test"
    ) as client:
        yield GatewayTestEnv(client=client, downstreams=downstreams, clock=clock)

    await http.aclose()
