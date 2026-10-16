"""Gateway reverse proxy: header injection, passthrough, allowlist and rate limiting."""
import json

import httpx
import pytest

from common.auth import create_access_token
from tests.conftest import GATEWAY_RATE_LIMIT_CAPACITY
from tests.gateway_support import (
    CATALOG_PORT,
    ENROLLMENT_PORT,
    STUDENT_PORT,
    GatewayTestEnv,
)


def bearer(student_id: str = "STU_001") -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(student_id)}"}


async def test_injects_verified_student_id_and_keeps_token_at_gateway(
    gateway_env: GatewayTestEnv,
):
    res = await gateway_env.client.post(
        "/api/v1/enrollments", json={"section_id": 101}, headers=bearer("STU_001")
    )

    assert res.status_code == 200
    sent = gateway_env.downstreams.last
    assert (sent.method, sent.url.port, sent.url.path) == ("POST", ENROLLMENT_PORT, "/enrollments")
    assert sent.headers["X-Student-Id"] == "STU_001"
    assert "authorization" not in sent.headers
    assert json.loads(sent.content) == {"section_id": 101}


async def test_spoofed_student_id_header_is_overwritten(gateway_env: GatewayTestEnv):
    await gateway_env.client.get(
        "/api/v1/enrollments",
        headers={**bearer("STU_001"), "X-Student-Id": "STU_002"},
    )

    assert gateway_env.downstreams.last.headers.get_list("X-Student-Id") == ["STU_001"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (201, {"status": "ENROLLED", "section_id": 101}),
        (409, {"status": "WAITLISTED", "section_id": 101}),
        (400, {"status": "REJECTED", "message": "Financial hold"}),
        (404, {"detail": "Section 101 not found."}),
    ],
)
async def test_upstream_status_and_body_pass_through(
    gateway_env: GatewayTestEnv, status: int, body: dict
):
    gateway_env.downstreams.respond("POST", ENROLLMENT_PORT, "/enrollments", status, body)

    res = await gateway_env.client.post(
        "/api/v1/enrollments", json={"section_id": 101}, headers=bearer()
    )

    assert res.status_code == status
    assert res.json() == body
    assert res.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("method", "public_path", "port", "upstream_path"),
    [
        ("GET", "/api/v1/enrollments", ENROLLMENT_PORT, "/enrollments"),
        ("GET", "/api/v1/enrollments/me", ENROLLMENT_PORT, "/enrollments/me"),
        ("GET", "/api/v1/waitlist/me", ENROLLMENT_PORT, "/waitlist/me"),
        ("GET", "/api/v1/courses", CATALOG_PORT, "/courses"),
        ("GET", "/api/v1/courses/CS301", CATALOG_PORT, "/courses/CS301"),
        ("GET", "/api/v1/sections", CATALOG_PORT, "/sections"),
        ("GET", "/api/v1/sections/101", CATALOG_PORT, "/sections/101"),
        ("GET", "/api/v1/students/me", STUDENT_PORT, "/students/STU_001"),
        (
            "GET",
            "/api/v1/students/me/eligibility",
            STUDENT_PORT,
            "/students/STU_001/eligibility",
        ),
    ],
)
async def test_routes_reach_the_right_service(
    gateway_env: GatewayTestEnv, method: str, public_path: str, port: int, upstream_path: str
):
    res = await gateway_env.client.request(method, public_path, headers=bearer("STU_001"))

    assert res.status_code == 200
    sent = gateway_env.downstreams.last
    assert (sent.method, sent.url.port, sent.url.path) == (method, port, upstream_path)


async def test_drop_is_forwarded(gateway_env: GatewayTestEnv):
    await gateway_env.client.post(
        "/api/v1/drops", json={"section_id": 101}, headers=bearer()
    )

    sent = gateway_env.downstreams.last
    assert (sent.url.port, sent.url.path) == (ENROLLMENT_PORT, "/drops")
    assert json.loads(sent.content) == {"section_id": 101}


async def test_claim_is_forwarded(gateway_env: GatewayTestEnv):
    await gateway_env.client.post(
        "/api/v1/waitlist/claim", json={"offer_id": 7}, headers=bearer("STU_001")
    )

    sent = gateway_env.downstreams.last
    assert (sent.method, sent.url.port, sent.url.path) == (
        "POST",
        ENROLLMENT_PORT,
        "/waitlist/claim",
    )
    assert sent.headers["X-Student-Id"] == "STU_001"
    assert json.loads(sent.content) == {"offer_id": 7}


async def test_query_string_is_forwarded(gateway_env: GatewayTestEnv):
    await gateway_env.client.get(
        "/api/v1/sections?course_code=CS301&open_only=true", headers=bearer()
    )

    assert gateway_env.downstreams.last.url.query == b"course_code=CS301&open_only=true"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/students/verify-credentials"),
        ("POST", "/api/v1/students"),
        ("GET", "/api/v1/students/STU_002"),
        ("PATCH", "/api/v1/students/STU_001/hold"),
        ("POST", "/api/v1/sections/101/seats/reserve"),
        ("POST", "/api/v1/courses"),
        ("PATCH", "/api/v1/sections/101/capacity"),
    ],
)
async def test_internal_endpoints_are_not_exposed(
    gateway_env: GatewayTestEnv, method: str, path: str
):
    res = await gateway_env.client.request(method, path, json={}, headers=bearer())

    assert res.status_code in (404, 405)
    assert gateway_env.downstreams.requests == []


async def test_invalid_body_is_rejected_before_forwarding(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.post(
        "/api/v1/enrollments", json={"section_id": "abc"}, headers=bearer()
    )

    assert res.status_code == 422
    assert gateway_env.downstreams.requests == []


async def test_rate_limit_is_per_student(gateway_env: GatewayTestEnv):
    for _ in range(GATEWAY_RATE_LIMIT_CAPACITY):
        ok = await gateway_env.client.get("/api/v1/enrollments", headers=bearer("STU_001"))
        assert ok.status_code == 200

    limited = await gateway_env.client.get("/api/v1/enrollments", headers=bearer("STU_001"))
    other = await gateway_env.client.get("/api/v1/enrollments", headers=bearer("STU_002"))

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert other.status_code == 200
    assert len(gateway_env.downstreams.requests) == GATEWAY_RATE_LIMIT_CAPACITY + 1


async def test_rate_limit_recovers_after_refill(gateway_env: GatewayTestEnv):
    for _ in range(GATEWAY_RATE_LIMIT_CAPACITY + 1):
        res = await gateway_env.client.get("/api/v1/enrollments", headers=bearer())
    assert res.status_code == 429

    gateway_env.clock.advance(1.0)
    res = await gateway_env.client.get("/api/v1/enrollments", headers=bearer())

    assert res.status_code == 200


async def test_unreachable_upstream_returns_502(gateway_env: GatewayTestEnv):
    gateway_env.downstreams.error = httpx.ConnectError("connection refused")

    res = await gateway_env.client.get("/api/v1/enrollments", headers=bearer())

    assert res.status_code == 502


async def test_upstream_timeout_returns_504(gateway_env: GatewayTestEnv):
    gateway_env.downstreams.error = httpx.ReadTimeout("slow")

    res = await gateway_env.client.get("/api/v1/enrollments", headers=bearer())

    assert res.status_code == 504
