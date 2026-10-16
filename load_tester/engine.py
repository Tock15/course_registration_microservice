"""
load_tester/engine.py
High-concurrency load testing engine with real-time telemetry,
Pydantic v2 metrics, and Zero-Overbooking invariant verification.
"""
import asyncio
import logging
import os
import statistics
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from common.auth import create_access_token

logger = logging.getLogger(__name__)


class LoadTestConfig(BaseModel):
    """Configuration options for a load test run."""

    target_url: str = Field(
        default_factory=lambda: os.getenv("GATEWAY_URL", "http://localhost:8000")
    )
    catalog_url: str = Field(
        default_factory=lambda: os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")
    )
    student_url: str = Field(
        default_factory=lambda: os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")
    )
    target_type: str = Field(
        default="gateway",
        description="'gateway' (Bearer JWT via :8000) or 'direct' (X-Student-Id via :8003)",
    )
    section_id: int = Field(default=101)
    total_requests: int = Field(default=100, ge=1, le=1000)
    concurrency: int = Field(default=20, ge=1, le=200)
    timeout_seconds: float = Field(default=15.0, ge=1.0)
    auto_provision_students: bool = Field(default=True)
    student_prefix: str = Field(default="STU_LOAD_")


class RequestResult(BaseModel):
    """Individual HTTP request result with high-precision timing."""

    index: int
    student_id: str
    status_code: int
    category: str  # SUCCESS, WAITLISTED, RATE_LIMITED, REJECTED, ERROR
    latency_ms: float
    message: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
    )


class LoadTestSummary(BaseModel):
    """Aggregated outcome of a completed or ongoing load test."""

    total_requests: int
    completed_requests: int
    succeeded_count: int
    waitlisted_count: int
    rate_limited_count: int
    rejected_count: int
    error_count: int
    total_failed_count: int

    # Timing & Throughput
    duration_seconds: float
    requests_per_second: float
    latency_min_ms: float
    latency_avg_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_max_ms: float

    # Invariant Verification
    section_id: int
    initial_enrolled: int | None = None
    final_enrolled: int | None = None
    section_capacity: int | None = None
    zero_overbooking_passed: bool | None = None
    invariant_detail: str | None = None

    # Granular request log (capped to avoid payload bloat if large)
    results: list[RequestResult] = Field(default_factory=list)


async def fetch_section_info(
    client: httpx.AsyncClient, catalog_url: str, section_id: int
) -> dict[str, Any] | None:
    """Fetch live section telemetry directly from Catalog Service."""
    try:
        res = await client.get(f"{catalog_url.rstrip('/')}/sections/{section_id}", timeout=5.0)
        if res.status_code == 200:
            return res.json()
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Could not fetch section %s: %s", section_id, exc)
    return None


async def provision_student_cohort(
    client: httpx.AsyncClient,
    student_url: str,
    student_ids: list[str],
    prereqs: list[str] | None = None,
) -> int:
    """Ensure student profiles exist with fulfilled prerequisites."""
    if prereqs is None:
        prereqs = ["CS101", "CS102", "CS201", "CS202"]

    if not student_ids:
        return 0

    base_url = student_url.rstrip("/")
    # Fast check: if the first and last students already exist, skip cohort creation
    try:
        first_res = await client.get(f"{base_url}/students/{student_ids[0]}", timeout=3.0)
        last_res = await client.get(f"{base_url}/students/{student_ids[-1]}", timeout=3.0)
        if first_res.status_code == 200 and last_res.status_code == 200:
            logger.info("Student cohort already provisioned; skipping creation.")
            return len(student_ids)
    except (httpx.HTTPError, OSError):
        pass

    provisioned = 0
    url = f"{base_url}/students"

    async def _provision_one(sid: str) -> None:
        nonlocal provisioned
        payload = {
            "id": sid,
            "name": f"Test Student {sid}",
            "email": f"{sid.lower()}@loadtest.university.edu",
            "major": "Computer Engineering",
            "year": 4,
            "gpa": 3.75,
            "academic_standing": "GOOD_STANDING",
            "financial_hold": False,
            "completed_courses": prereqs,
            "enrolled_credits": 0,
            "max_credits": 22,
            "password": "password123",
        }
        try:
            res = await client.post(url, json=payload, timeout=3.0)
            if res.status_code == 201:
                provisioned += 1
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Student provision skipped for %s: %s", sid, exc)

    # Increased batch size to 50 for faster provisioning
    batch_size = 50
    for i in range(0, len(student_ids), batch_size):
        chunk = student_ids[i : i + batch_size]
        await asyncio.gather(*(_provision_one(sid) for sid in chunk))

    return provisioned


async def execute_load_test(
    config: LoadTestConfig,
    on_progress: Callable[[RequestResult, int, int], None] | None = None,
) -> LoadTestSummary:
    """Execute the configured load test and compute detailed telemetry."""
    student_ids = [
        f"{config.student_prefix}{i:03d}" for i in range(1, config.total_requests + 1)
    ]

    limits = httpx.Limits(max_connections=500, max_keepalive_connections=200)
    async with httpx.AsyncClient(limits=limits, timeout=config.timeout_seconds) as client:
        # 1. Fetch initial section state from Catalog if reachable
        initial_sec = await fetch_section_info(client, config.catalog_url, config.section_id)
        init_enrolled = initial_sec.get("enrolled_count") if initial_sec else None
        capacity = initial_sec.get("capacity") if initial_sec else None

        # 2. Auto-provision students if requested
        if config.auto_provision_students:
            await provision_student_cohort(client, config.student_url, student_ids)

        # 3. Prepare requests
        queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for idx, sid in enumerate(student_ids):
            await queue.put((idx + 1, sid))

        results: list[RequestResult] = []
        results_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(config.concurrency)

        start_time = time.perf_counter()

        async def worker() -> None:
            while not queue.empty():
                try:
                    req_idx, sid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                async with semaphore:
                    req_start = time.perf_counter()
                    category = "ERROR"
                    msg = ""
                    status_code = 0

                    try:
                        if config.target_type == "gateway":
                            token = create_access_token(sid)
                            headers = {
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            }
                            url = f"{config.target_url.rstrip('/')}/api/v1/enrollments"
                        else:
                            headers = {
                                "X-Student-Id": sid,
                                "Content-Type": "application/json",
                            }
                            url = f"{config.target_url.rstrip('/')}/enrollments"

                        resp = await client.post(
                            url, json={"section_id": config.section_id}, headers=headers
                        )
                        req_end = time.perf_counter()
                        latency_ms = (req_end - req_start) * 1000.0
                        status_code = resp.status_code

                        try:
                            body = resp.json()
                        except ValueError:
                            body = {}

                        if status_code == 201 or (
                            status_code == 200 and body.get("status") == "ENROLLED"
                        ):
                            category = "SUCCESS"
                            msg = body.get("message", "Enrolled successfully.")
                        elif status_code == 409 and body.get("status") == "WAITLISTED":
                            category = "WAITLISTED"
                            pos = body.get("waitlist_position", "?")
                            msg = f"Waitlisted at position #{pos}"
                        elif status_code == 429:
                            category = "RATE_LIMITED"
                            msg = "Rate limit exceeded (Token Bucket)."
                        elif 400 <= status_code < 500:
                            category = "REJECTED"
                            msg = (
                                body.get("message")
                                or body.get("detail")
                                or f"Client rejected (HTTP {status_code})"
                            )
                        else:
                            category = "ERROR"
                            msg = body.get("detail") or f"Server error (HTTP {status_code})"

                    except httpx.TimeoutException:
                        req_end = time.perf_counter()
                        latency_ms = (req_end - req_start) * 1000.0
                        status_code = 504
                        category = "ERROR"
                        msg = "Request timed out."
                    except (httpx.HTTPError, OSError) as exc:
                        req_end = time.perf_counter()
                        latency_ms = (req_end - req_start) * 1000.0
                        status_code = 502
                        category = "ERROR"
                        msg = f"Connection failed: {type(exc).__name__}"

                    record = RequestResult(
                        index=req_idx,
                        student_id=sid,
                        status_code=status_code,
                        category=category,
                        latency_ms=round(latency_ms, 2),
                        message=str(msg),
                    )

                    async with results_lock:
                        results.append(record)
                        completed = len(results)

                    if on_progress:
                        try:
                            on_progress(record, completed, config.total_requests)
                        except (RuntimeError, ValueError) as exc:
                            logger.debug("Progress callback error: %s", exc)

                queue.task_done()

        # Run concurrent workers
        worker_count = min(config.concurrency, config.total_requests)
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)

        end_time = time.perf_counter()
        total_duration = max(0.001, end_time - start_time)

        # 4. Fetch final section telemetry to verify Zero Overbooking Invariant
        final_sec = await fetch_section_info(client, config.catalog_url, config.section_id)
        final_enrolled = final_sec.get("enrolled_count") if final_sec else None
        if capacity is None and final_sec:
            capacity = final_sec.get("capacity")

        # 5. Compute statistics
        succeeded = sum(1 for r in results if r.category == "SUCCESS")
        waitlisted = sum(1 for r in results if r.category == "WAITLISTED")
        rate_limited = sum(1 for r in results if r.category == "RATE_LIMITED")
        rejected = sum(1 for r in results if r.category == "REJECTED")
        errors = sum(1 for r in results if r.category == "ERROR")
        total_failed = rate_limited + rejected + errors

        latencies = [r.latency_ms for r in results] if results else [0.0]
        lat_min = min(latencies)
        lat_max = max(latencies)
        lat_avg = statistics.mean(latencies)
        lat_p50 = statistics.median(latencies)
        lat_p95 = (
            statistics.quantiles(latencies, n=20)[18]
            if len(latencies) >= 20
            else lat_max
        )

        # Zero-overbooking check
        zero_passed = None
        detail = None
        if capacity is not None and final_enrolled is not None:
            if final_enrolled <= capacity:
                zero_passed = True
                detail = (
                    f"PASS: Final enrolled ({final_enrolled}) strictly respects "
                    f"section capacity ({capacity}). Zero overbooking guaranteed."
                )
            else:
                zero_passed = False
                detail = (
                    f"VIOLATION: Final enrolled ({final_enrolled}) exceeded "
                    f"capacity ({capacity})!"
                )

        return LoadTestSummary(
            total_requests=config.total_requests,
            completed_requests=len(results),
            succeeded_count=succeeded,
            waitlisted_count=waitlisted,
            rate_limited_count=rate_limited,
            rejected_count=rejected,
            error_count=errors,
            total_failed_count=total_failed,
            duration_seconds=round(total_duration, 3),
            requests_per_second=round(len(results) / total_duration, 1),
            latency_min_ms=round(lat_min, 2),
            latency_avg_ms=round(lat_avg, 2),
            latency_p50_ms=round(lat_p50, 2),
            latency_p95_ms=round(lat_p95, 2),
            latency_max_ms=round(lat_max, 2),
            section_id=config.section_id,
            initial_enrolled=init_enrolled,
            final_enrolled=final_enrolled,
            section_capacity=capacity,
            zero_overbooking_passed=zero_passed,
            invariant_detail=detail,
            results=results,
        )


async def stream_load_test(
    config: LoadTestConfig,
) -> AsyncGenerator[dict[str, Any], None]:
    """Async generator yielding real-time SSE progress events as requests finish."""
    events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def progress_callback(item: RequestResult, completed: int, total: int) -> None:
        events_queue.put_nowait(
            {
                "type": "progress",
                "completed": completed,
                "total": total,
                "item": item.model_dump(),
            }
        )

    task = asyncio.create_task(execute_load_test(config, on_progress=progress_callback))

    while not task.done() or not events_queue.empty():
        try:
            event = await asyncio.wait_for(events_queue.get(), timeout=0.1)
            yield event
        except TimeoutError:
            continue

    summary = await task
    yield {"type": "complete", "summary": summary.model_dump()}
