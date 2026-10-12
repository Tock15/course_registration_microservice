"""
enrollment_service/tests/test_e2e_stress.py
End-to-end zero-overbooking stress test through the real API Gateway.

Skipped unless RUN_E2E=1, because it needs every service running:
  - API Gateway (:8000) routing /api/v1/enrollments -> :8003 with X-Student-Id
  - Student Service (:8001) with students STU_000..STU_099 who completed
    CS102 and CS202
  - Catalog Service (:8002) freshly seeded, including the seat endpoints
    POST /sections/{id}/seats/reserve|release
  - Enrollment Service (:8003) with an empty enrollment.db

Run:  RUN_E2E=1 uv run pytest enrollment_service/tests/test_e2e_stress.py -v
"""
import asyncio
import os

import httpx
import pytest

from common.auth import create_access_token

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_E2E") != "1",
    reason="End-to-end test; start all services and set RUN_E2E=1.",
)

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
CATALOG_URL = os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")
SECTION_ID = 101
TOTAL_STUDENTS = 100
CAPACITY = 10


async def test_concurrent_registration_zero_overbooking():
    tokens = [create_access_token(f"STU_{i:03d}") for i in range(TOTAL_STUDENTS)]

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/enrollments",
                    json={"section_id": SECTION_ID},
                    headers={"Authorization": f"Bearer {token}"},
                )
                for token in tokens
            )
        )

    enrolled = [r for r in responses if r.status_code == 201 and r.json()["status"] == "ENROLLED"]
    waitlisted = [
        r for r in responses if r.status_code == 409 and r.json().get("status") == "WAITLISTED"
    ]
    assert len(enrolled) == CAPACITY, f"Expected {CAPACITY} enrolled, got {len(enrolled)}"
    assert len(waitlisted) == TOTAL_STUDENTS - CAPACITY

    async with httpx.AsyncClient(base_url=CATALOG_URL, timeout=10.0) as catalog:
        section = (await catalog.get(f"/sections/{SECTION_ID}")).json()
    assert section["enrolled_count"] == CAPACITY <= section["capacity"]
