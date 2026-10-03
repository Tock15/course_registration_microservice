"""
tests/test_services_health.py
Baseline health check tests verifying that all 5 microservice skeletons run.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from api_gateway.main import app as gateway_app
from catalog_service.main import app as catalog_app
from enrollment_service.main import app as enrollment_app
from notification_service.main import app as notification_app
from student_service.main import app as student_app


@pytest.mark.asyncio
async def test_api_gateway_health():
    async with AsyncClient(
        transport=ASGITransport(app=gateway_app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "api_gateway"
        assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_student_service_health():
    async with AsyncClient(
        transport=ASGITransport(app=student_app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "student_service"
        assert response.json()["port"] == 8001


@pytest.mark.asyncio
async def test_catalog_service_health():
    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "catalog_service"
        assert response.json()["port"] == 8002


@pytest.mark.asyncio
async def test_enrollment_service_health():
    async with AsyncClient(
        transport=ASGITransport(app=enrollment_app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "enrollment_service"
        assert response.json()["port"] == 8003


@pytest.mark.asyncio
async def test_notification_service_health():
    async with AsyncClient(
        transport=ASGITransport(app=notification_app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "notification_service"
        assert response.json()["port"] == 8004
