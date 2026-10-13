"""Enrollment Service API (port 8003)."""
import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.schemas import DropRequest, EnrollmentRequest, EnrollmentStatus
from enrollment_service.clients import (
    CatalogClient,
    StudentClient,
    UpstreamNotFound,
    UpstreamUnavailable,
)
from enrollment_service.config import (
    CATALOG_SERVICE_URL,
    HTTP_TIMEOUT_SECONDS,
    NOTIFICATION_SERVICE_URL,
    RECONCILE_INTERVAL_SECONDS,
    STUDENT_SERVICE_URL,
)
from enrollment_service.database import async_session_maker, get_session_maker, init_db
from enrollment_service.events import EventPublisher, HttpEventPublisher
from enrollment_service.orchestrator import EnrollmentOrchestrator
from enrollment_service.reconciler import Reconciler
from enrollment_service.schemas import EnrollmentResult, MyEnrollmentsResponse
from enrollment_service.seat_allocator import DuplicateEnrollmentError
from enrollment_service.waitlist import NotEnrolledError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the schema, shared HTTP client and background reconciler."""
    await init_db()

    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    app.state.student_client = StudentClient(http, STUDENT_SERVICE_URL)
    app.state.catalog_client = CatalogClient(http, CATALOG_SERVICE_URL)
    app.state.event_publisher = HttpEventPublisher(http, NOTIFICATION_SERVICE_URL)

    reconciler = Reconciler(
        async_session_maker, app.state.catalog_client, app.state.event_publisher
    )
    reconciler_task = asyncio.create_task(
        reconciler.run_forever(RECONCILE_INTERVAL_SECONDS)
    )
    try:
        yield
    finally:
        reconciler_task.cancel()
        with suppress(asyncio.CancelledError):
            await reconciler_task
        await app.state.event_publisher.aclose()
        await http.aclose()


app = FastAPI(
    title="Enrollment Service",
    description="Core registration engine featuring atomic seat allocation, rule validation, and waitlists.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_student_client(request: Request) -> StudentClient:
    return request.app.state.student_client


def get_catalog_client(request: Request) -> CatalogClient:
    return request.app.state.catalog_client


def get_event_publisher(request: Request) -> EventPublisher:
    return request.app.state.event_publisher


def get_current_student(
    x_student_id: str | None = Header(
        default=None,
        alias="X-Student-Id",
        description="Authenticated student ID, injected by the API Gateway.",
    ),
) -> str:
    if not x_student_id or not x_student_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Student-Id header; requests must come through the API Gateway.",
        )
    return x_student_id.strip()


def get_orchestrator(
    sessions: async_sessionmaker[AsyncSession] = Depends(get_session_maker),
    student: StudentClient = Depends(get_student_client),
    catalog: CatalogClient = Depends(get_catalog_client),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> EnrollmentOrchestrator:
    return EnrollmentOrchestrator(sessions, student, catalog, publisher)


@app.exception_handler(UpstreamNotFound)
async def upstream_not_found_handler(_: Request, exc: UpstreamNotFound):
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": exc.detail})


@app.exception_handler(UpstreamUnavailable)
async def upstream_unavailable_handler(_: Request, exc: UpstreamUnavailable):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": f"{exc.service} is unavailable; please retry. ({exc.detail})"},
    )


@app.exception_handler(DuplicateEnrollmentError)
async def duplicate_enrollment_handler(_: Request, exc: DuplicateEnrollmentError):
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(NotEnrolledError)
async def not_enrolled_handler(_: Request, exc: NotEnrolledError):
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(DBAPIError)
async def db_busy_handler(_: Request, exc: DBAPIError):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database busy under peak registration load; please retry."},
        headers={"Retry-After": "1"},
    )


@app.post("/admin/reset-cache", tags=["Admin"])
async def reset_cache(orchestrator: EnrollmentOrchestrator = Depends(get_orchestrator)):
    """Reset in-memory seat saturation cache."""
    orchestrator.reset_cache()
    return {"ok": True, "message": "Enrollment saturation cache cleared."}


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for service monitoring and API Gateway routing."""
    return {
        "service": "enrollment_service",
        "status": "healthy",
        "port": 8003,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "Enrollment Engine Service",
        "version": "0.1.0",
        "docs_url": "/docs",
    }


_ENROLL_STATUS_CODES = {
    EnrollmentStatus.ENROLLED: status.HTTP_201_CREATED,
    EnrollmentStatus.WAITLISTED: status.HTTP_409_CONFLICT,
    EnrollmentStatus.REJECTED: status.HTTP_400_BAD_REQUEST,
}


@app.post(
    "/enrollments",
    response_model=EnrollmentResult,
    status_code=status.HTTP_201_CREATED,
    tags=["Enrollments"],
    responses={
        400: {"model": EnrollmentResult, "description": "REJECTED by a validation rule"},
        401: {"description": "Missing X-Student-Id header"},
        404: {"description": "Student or section not found"},
        409: {
            "model": EnrollmentResult,
            "description": "Section full: WAITLISTED (or a duplicate request)",
        },
        503: {"description": "Student or Catalog service unavailable"},
    },
)
async def enroll(
    payload: EnrollmentRequest,
    student_id: str = Depends(get_current_student),
    orchestrator: EnrollmentOrchestrator = Depends(get_orchestrator),
):
    """Enroll in a section, or join its waitlist if it is full."""
    result = await orchestrator.enroll(student_id, payload.section_id)
    code = _ENROLL_STATUS_CODES[result.status]
    if code == status.HTTP_201_CREATED:
        return result
    return JSONResponse(status_code=code, content=result.model_dump(mode="json"))


@app.post(
    "/drops",
    response_model=EnrollmentResult,
    tags=["Enrollments"],
    responses={404: {"description": "No active enrollment or waitlist entry"}},
)
async def drop(
    payload: DropRequest,
    student_id: str = Depends(get_current_student),
    orchestrator: EnrollmentOrchestrator = Depends(get_orchestrator),
):
    """Drop a section or leave its waitlist; a freed seat goes to the queue head."""
    return await orchestrator.drop(student_id, payload.section_id)


@app.get("/enrollments", response_model=MyEnrollmentsResponse, tags=["Enrollments"])
async def my_enrollments(
    student_id: str = Depends(get_current_student),
    orchestrator: EnrollmentOrchestrator = Depends(get_orchestrator),
):
    """List the caller's enrollments (newest first) with waitlist positions."""
    return await orchestrator.list_for_student(student_id)
