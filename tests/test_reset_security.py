"""Tests for /reset endpoint security guards across services."""
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from catalog_service.main import app as catalog_app
from enrollment_service.main import app as enrollment_app
from student_service.main import app as student_app


@pytest.mark.asyncio
async def test_catalog_reset_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_RESET", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 403
        assert "disabled" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_student_reset_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_RESET", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=student_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 403
        assert "disabled" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_enrollment_reset_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_RESET", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=enrollment_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 403
        assert "disabled" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_allowed_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("ALLOW_RESET", "true")
    monkeypatch.setattr(
        "enrollment_service.main.reset_enrollment_data",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "catalog_service.main.seed_catalog_data",
        AsyncMock(return_value={"courses": 5}),
    )
    monkeypatch.setattr(
        "student_service.main.seed_students",
        AsyncMock(return_value={"students": 7}),
    )

    async with AsyncClient(
        transport=ASGITransport(app=enrollment_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 200
        assert res.json()["ok"] is True

    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 200
        assert res.json()["ok"] is True

    async with AsyncClient(
        transport=ASGITransport(app=student_app), base_url="http://test"
    ) as client:
        res = await client.post("/reset")
        assert res.status_code == 200
        assert res.json()["ok"] is True
