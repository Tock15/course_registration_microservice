"""
tests/test_load_tester.py
Integration and unit tests verifying the load test web interface and engine.
"""
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response

from load_tester.engine import LoadTestConfig, execute_load_test
from load_tester.main import app


@pytest.mark.asyncio
async def test_dashboard_ui_returns_html():
    """Verify that the dashboard endpoint serves the HTML interface."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "Course Registration Concurrency Bench" in res.text
        assert "Zero Overbooking" in res.text
        assert "cardSuccess" in res.text
        assert "cardFailed" in res.text


@pytest.mark.asyncio
async def test_load_tester_health():
    """Verify load tester health check endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/health")
        assert res.status_code == 200
        assert res.json()["service"] == "load_tester_ui"
        assert res.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_services_status_endpoint():
    """Verify services status ping endpoint returns all 5 service entries."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/api/services-status")
        assert res.status_code == 200
        data = res.json()
        assert set(data.keys()) == {
            "gateway",
            "student",
            "catalog",
            "enrollment",
            "notification",
        }
        for item in data.values():
            assert "name" in item
            assert "online" in item
            assert "latency_ms" in item


@pytest.mark.asyncio
async def test_engine_execution_with_mock_gateway(monkeypatch):
    """Verify load test engine computes successes, waitlists, and failures correctly."""
    enrollment_calls = 0

    def mock_handler(request):
        nonlocal enrollment_calls
        path = request.url.path

        if "/sections/" in path:
            return Response(200, json={"id": 101, "capacity": 3, "enrolled_count": 3})

        if "/students" in path:
            return Response(201, json={"id": "STU_LOAD_001"})

        if "/api/v1/enrollments" in path or "/enrollments" in path:
            enrollment_calls += 1
            # Simulate: 1st success, 2nd waitlisted, 3rd rate-limited, 4th rejected, 5th error
            if enrollment_calls == 1:
                return Response(201, json={"status": "ENROLLED", "message": "Enrolled!"})
            elif enrollment_calls == 2:
                return Response(
                    409,
                    json={"status": "WAITLISTED", "waitlist_position": 1},
                )
            elif enrollment_calls == 3:
                return Response(429, json={"detail": "Too many requests"})
            elif enrollment_calls == 4:
                return Response(400, json={"message": "Missing prerequisites"})
            else:
                return Response(500, json={"detail": "Internal error"})

        return Response(404)

    mock_client = AsyncClient(transport=MockTransport(mock_handler))
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: mock_client)

    config = LoadTestConfig(
        target_url="http://test-gw:8000",
        catalog_url="http://test-catalog:8002",
        student_url="http://test-student:8001",
        section_id=101,
        total_requests=5,
        concurrency=1,
        auto_provision_students=False,
    )

    summary = await execute_load_test(config)

    assert summary.total_requests == 5
    assert summary.completed_requests == 5
    assert summary.succeeded_count == 1
    assert summary.waitlisted_count == 1
    assert summary.rate_limited_count == 1
    assert summary.rejected_count == 1
    assert summary.error_count == 1
    assert summary.total_failed_count == 3  # rate_limited + rejected + error
    assert summary.zero_overbooking_passed is True
    assert len(summary.results) == 5
    assert summary.requests_per_second > 0
