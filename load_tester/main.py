"""
load_tester/main.py
FastAPI Web Interface & Dashboard for high-concurrency load testing.
Port: 8080 (or custom).
"""
import asyncio
import os
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from load_tester.engine import (
    LoadTestConfig,
    LoadTestSummary,
    execute_load_test,
    provision_student_cohort,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
INDEX_HTML_PATH = TEMPLATES_DIR / "index.html"

app = FastAPI(
    title="Load Test Bench Web Interface",
    description="Interactive Web UI and concurrency bench for Student Course Registration System.",
    version="0.1.0",
)


class ServiceHealthItem(BaseModel):
    name: str
    url: str
    online: bool
    status: str | None = None
    latency_ms: float = 0.0


class ResetResponse(BaseModel):
    ok: bool
    message: str
    details: dict[str, str] = Field(default_factory=dict)


class ProvisionRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=500)
    prefix: str = Field(default="STU_LOAD_")
    student_url: str = Field(default="http://localhost:8001")


@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def get_dashboard() -> HTMLResponse:
    """Serve the single-page load testing web dashboard."""
    if not INDEX_HTML_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboard template index.html not found.",
        )
    content = INDEX_HTML_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Health check for the load tester service itself."""
    return {"service": "load_tester_ui", "status": "healthy", "port": "8080"}


@app.get(
    "/api/services-status",
    response_model=dict[str, ServiceHealthItem],
    tags=["Telemetry"],
)
async def check_all_services_status() -> dict[str, ServiceHealthItem]:
    """Ping all 5 microservices to determine operational readiness."""
    services = {
        "gateway": ("API Gateway", os.getenv("GATEWAY_URL", "http://localhost:8000")),
        "student": ("Student Service", os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")),
        "catalog": ("Catalog Service", os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")),
        "enrollment": (
            "Enrollment Service",
            os.getenv("ENROLLMENT_SERVICE_URL", "http://localhost:8003"),
        ),
        "notification": (
            "Notification Service",
            os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8004"),
        ),
    }

    results: dict[str, ServiceHealthItem] = {}

    async with httpx.AsyncClient(timeout=2.0) as client:
        async def _ping(key: str, label: str, base_url: str) -> None:
            t0 = time.perf_counter()
            try:
                res = await client.get(f"{base_url.rstrip('/')}/health")
                t1 = time.perf_counter()
                latency = round((t1 - t0) * 1000.0, 1)
                is_ok = res.status_code == 200
                results[key] = ServiceHealthItem(
                    name=label,
                    url=base_url,
                    online=is_ok,
                    status="healthy" if is_ok else f"HTTP {res.status_code}",
                    latency_ms=latency,
                )
            except (httpx.HTTPError, OSError):
                t1 = time.perf_counter()
                results[key] = ServiceHealthItem(
                    name=label,
                    url=base_url,
                    online=False,
                    status="unreachable",
                    latency_ms=round((t1 - t0) * 1000.0, 1),
                )

        await asyncio.gather(
            *(_ping(k, lbl, url) for k, (lbl, url) in services.items())
        )

    return results


@app.post("/api/run", response_model=LoadTestSummary, tags=["Load Test"])
async def run_load_test(config: LoadTestConfig) -> LoadTestSummary:
    """Trigger an asynchronous high-concurrency load test run."""
    return await execute_load_test(config)


@app.post("/api/provision-students", tags=["Provisioning"])
async def provision_students(payload: ProvisionRequest) -> dict[str, int]:
    """Auto-provision test students in Student Service with fulfilled prerequisites."""
    student_ids = [f"{payload.prefix}{i:03d}" for i in range(1, payload.count + 1)]
    async with httpx.AsyncClient() as client:
        count = await provision_student_cohort(client, payload.student_url, student_ids)
    return {"requested": payload.count, "provisioned": count}


@app.post("/api/reset", response_model=ResetResponse, tags=["Admin"])
async def reset_databases() -> ResetResponse:
    """Reset databases (Catalog, Enrollment, Student) to seed baseline states."""
    details: dict[str, str] = {}

    # 1. Reset enrollment.db
    try:
        from enrollment_service.database import engine as enroll_engine
        from enrollment_service.seed import reset_enrollment_data

        await reset_enrollment_data(enroll_engine)
        details["enrollment"] = "Recreated tables: enrollments, audit_log (clean)."
    except (SQLAlchemyError, OSError) as exc:
        details["enrollment"] = f"Reset failed: {exc}"

    # 2. Reset catalog.db
    try:
        from catalog_service.database import async_session_maker as catalog_sessions
        from catalog_service.database import init_db as init_catalog_db
        from catalog_service.seed import seed_catalog_data

        await init_catalog_db()
        async with catalog_sessions() as session:
            res = await seed_catalog_data(session)
            details["catalog"] = f"Seeded {res.get('sections_seeded', 0)} sections."
    except (SQLAlchemyError, OSError) as exc:
        details["catalog"] = f"Reset failed: {exc}"

    # 3. Reset student.db
    try:
        from student_service.database import async_session_maker as student_sessions
        from student_service.database import init_db as init_student_db
        from student_service.seed import seed_students

        await init_student_db()
        async with student_sessions() as session:
            res = await seed_students(session)
            details["student"] = f"Seeded {res.get('students_seeded', 0)} students."
    except (SQLAlchemyError, OSError) as exc:
        details["student"] = f"Reset failed: {exc}"

    return ResetResponse(
        ok=True,
        message="Databases reset to initial state.",
        details=details,
    )


def start() -> None:
    """Entry point for CLI runner."""
    port = int(os.getenv("LOAD_TEST_PORT", "8080"))
    uvicorn.run("load_tester.main:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    start()
