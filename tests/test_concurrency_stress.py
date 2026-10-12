"""End-to-end stress test through the gateway; runs only with RUN_E2E=1."""
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
