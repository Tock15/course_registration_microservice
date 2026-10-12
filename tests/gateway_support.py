"""Fakes for gateway tests: recorded downstream services and a manual clock."""
import json
from dataclasses import dataclass, field

import httpx

STUDENT_PORT = 8001
CATALOG_PORT = 8002
ENROLLMENT_PORT = 8003

VALID_PASSWORD = "password123"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeDownstreams:
    """httpx.MockTransport handler standing in for services :8001-:8003."""

    students: set[str] = field(default_factory=lambda: {"STU_001", "STU_002"})
    requests: list[httpx.Request] = field(default_factory=list)
    canned: dict[tuple[str, int, str], httpx.Response] = field(default_factory=dict)
    error: Exception | None = None

    def respond(self, method: str, port: int, path: str, status: int, body: dict) -> None:
        self.canned[(method, port, path)] = httpx.Response(status, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error

        key = (request.method, request.url.port, request.url.path)
        if key in self.canned:
            return self.canned[key]

        if key == ("POST", STUDENT_PORT, "/students/verify-credentials"):
            body = json.loads(request.content)
            student_id = body["student_id"].strip().upper()
            valid = student_id in self.students and body["password"] == VALID_PASSWORD
            return httpx.Response(
                200,
                json={
                    "valid": valid,
                    "student_id": student_id,
                    "role": "STUDENT",
                    "name": "Test Student" if valid else None,
                    "message": None,
                },
            )

        return httpx.Response(200, json={"ok": True})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@dataclass
class GatewayTestEnv:
    client: httpx.AsyncClient
    downstreams: FakeDownstreams
    clock: FakeClock
