"""Gateway login and bearer-token verification."""
from datetime import timedelta

import httpx
import jwt

from common.auth import ALGORITHM, create_access_token, decode_access_token
from tests.conftest import GATEWAY_RATE_LIMIT_CAPACITY
from tests.gateway_support import STUDENT_PORT, VALID_PASSWORD, GatewayTestEnv

LOGIN = "/api/v1/auth/login"
PROTECTED = "/api/v1/enrollments"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_login_issues_token_for_valid_credentials(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.post(
        LOGIN, json={"student_id": "stu_001", "password": VALID_PASSWORD}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["student_id"] == "STU_001"
    assert body["role"] == "STUDENT"
    claims = decode_access_token(body["access_token"])
    assert claims["sub"] == "STU_001"
    assert claims["role"] == "STUDENT"


async def test_login_token_is_accepted_by_protected_route(gateway_env: GatewayTestEnv):
    login = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_001", "password": VALID_PASSWORD}
    )
    res = await gateway_env.client.get(
        PROTECTED, headers=bearer(login.json()["access_token"])
    )

    assert res.status_code == 200
    assert gateway_env.downstreams.last.headers["X-Student-Id"] == "STU_001"


async def test_login_rejects_wrong_password(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_001", "password": "nope"}
    )

    assert res.status_code == 401
    assert "access_token" not in res.json()


async def test_login_rejects_unknown_student_with_same_message(gateway_env: GatewayTestEnv):
    wrong_password = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_001", "password": "nope"}
    )
    unknown = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_999", "password": VALID_PASSWORD}
    )

    assert unknown.status_code == 401
    assert unknown.json() == wrong_password.json()


async def test_login_returns_503_when_student_service_is_down(gateway_env: GatewayTestEnv):
    gateway_env.downstreams.error = httpx.ConnectError("connection refused")

    res = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_001", "password": VALID_PASSWORD}
    )

    assert res.status_code == 503


async def test_login_returns_503_when_student_service_errors(gateway_env: GatewayTestEnv):
    gateway_env.downstreams.respond(
        "POST", STUDENT_PORT, "/students/verify-credentials", 500, {"detail": "boom"}
    )

    res = await gateway_env.client.post(
        LOGIN, json={"student_id": "STU_001", "password": VALID_PASSWORD}
    )

    assert res.status_code == 503


async def test_login_is_rate_limited_per_client(gateway_env: GatewayTestEnv):
    creds = {"student_id": "STU_001", "password": "guess"}
    for _ in range(GATEWAY_RATE_LIMIT_CAPACITY):
        assert (await gateway_env.client.post(LOGIN, json=creds)).status_code == 401

    res = await gateway_env.client.post(LOGIN, json=creds)

    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert len(gateway_env.downstreams.requests) == GATEWAY_RATE_LIMIT_CAPACITY


async def test_missing_token_is_rejected(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.get(PROTECTED)

    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"] == "Bearer"
    assert gateway_env.downstreams.requests == []


async def test_garbage_token_is_rejected(gateway_env: GatewayTestEnv):
    res = await gateway_env.client.get(PROTECTED, headers=bearer("not-a-jwt"))

    assert res.status_code == 401
    assert gateway_env.downstreams.requests == []


async def test_expired_token_is_rejected(gateway_env: GatewayTestEnv):
    token = create_access_token("STU_001", expires_delta=timedelta(seconds=-1))

    res = await gateway_env.client.get(PROTECTED, headers=bearer(token))

    assert res.status_code == 401
    assert gateway_env.downstreams.requests == []


async def test_token_signed_with_another_key_is_rejected(gateway_env: GatewayTestEnv):
    forged = jwt.encode({"sub": "STU_001"}, "attacker-key-" + "x" * 32, algorithm=ALGORITHM)

    res = await gateway_env.client.get(PROTECTED, headers=bearer(forged))

    assert res.status_code == 401
    assert gateway_env.downstreams.requests == []
