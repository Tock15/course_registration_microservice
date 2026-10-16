"""
api_gateway/main.py
API Gateway & Protection Proxy (Port 8000).

Responsibilities for Teammate:
  - Centralized Authentication (POST /api/v1/auth/login) via auth.py & common.auth
  - Reverse Proxy routing to downstream microservices (:8001 - :8004)
  - Protection Proxy: Anti-spam token bucket rate limiting (rate_limiter.py)
  - Security boundary: Inject verified X-Student-Id header downstream
"""
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse

from api_gateway.auth import get_current_principal
from api_gateway.auth import router as auth_router
from api_gateway.config import (
    CATALOG_SERVICE_URL,
    ENROLLMENT_SERVICE_URL,
    HTTP_TIMEOUT_SECONDS,
    LOGIN_RATE_LIMIT_CAPACITY,
    LOGIN_RATE_LIMIT_REFILL_PER_SECOND,
    NOTIFICATION_SERVICE_URL,
    RATE_LIMIT_CAPACITY,
    RATE_LIMIT_REFILL_PER_SECOND,
    STUDENT_SERVICE_URL,
)
from api_gateway.proxy import HttpUpstream, ProtectionProxy, UpstreamService
from api_gateway.rate_limiter import RateLimitExceeded, TokenBucketRateLimiter
from common.schemas import DropRequest, EnrollmentRequest, TokenPayload


def wire_gateway(
    app: FastAPI,
    http: httpx.AsyncClient,
    limiter: TokenBucketRateLimiter | None = None,
    login_limiter: TokenBucketRateLimiter | None = None,
) -> None:
    """Attach the shared HTTP client, rate limiters and protected upstreams to the app."""
    # `is None`, not truthiness: an empty limiter has len() == 0.
    if limiter is None:
        limiter = TokenBucketRateLimiter(RATE_LIMIT_CAPACITY, RATE_LIMIT_REFILL_PER_SECOND)
    if login_limiter is None:
        login_limiter = TokenBucketRateLimiter(
            LOGIN_RATE_LIMIT_CAPACITY, LOGIN_RATE_LIMIT_REFILL_PER_SECOND
        )
    app.state.http = http
    app.state.login_limiter = login_limiter
    app.state.student_upstream = ProtectionProxy(
        HttpUpstream(http, STUDENT_SERVICE_URL, "student_service"), limiter
    )
    app.state.catalog_upstream = ProtectionProxy(
        HttpUpstream(http, CATALOG_SERVICE_URL, "catalog_service"), limiter
    )
    app.state.enrollment_upstream = ProtectionProxy(
        HttpUpstream(http, ENROLLMENT_SERVICE_URL, "enrollment_service"), limiter
    )
    app.state.notification_upstream = ProtectionProxy(
        HttpUpstream(http, NOTIFICATION_SERVICE_URL, "notification_service"), limiter
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the shared HTTP client used for every downstream call."""
    limits = httpx.Limits(
        max_connections=500,
        max_keepalive_connections=200,
        keepalive_expiry=30.0,
    )
    http = httpx.AsyncClient(limits=limits, timeout=HTTP_TIMEOUT_SECONDS)
    wire_gateway(app, http)
    try:
        yield
    finally:
        await http.aclose()


app = FastAPI(
    title="API Gateway & Security Proxy",
    description="Centralized ingress reverse proxy, rate limiter, and JWT authentication checkpoint.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(auth_router)


def get_student_upstream(request: Request) -> UpstreamService:
    return request.app.state.student_upstream


def get_catalog_upstream(request: Request) -> UpstreamService:
    return request.app.state.catalog_upstream


def get_enrollment_upstream(request: Request) -> UpstreamService:
    return request.app.state.enrollment_upstream


def get_notification_upstream(request: Request) -> UpstreamService:
    return request.app.state.notification_upstream


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(_: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many requests; please slow down."},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint for API Gateway."""
    return {
        "service": "api_gateway",
        "status": "healthy",
        "port": 8000,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": "API Gateway & Security Proxy",
        "version": "0.1.0",
        "docs_url": "/docs",
    }


# ---------------------------------------------------------
# Public route allowlist. Only the paths below are reachable from outside;
# downstream admin/internal endpoints are deliberately not exposed.
# The student ID always comes from the verified token, never the URL or body.
# ---------------------------------------------------------

_PROXIED_RESPONSES = {
    401: {"description": "Missing, invalid or expired bearer token"},
    429: {"description": "Rate limit exceeded"},
    502: {"description": "Downstream service unreachable"},
    504: {"description": "Downstream service timed out"},
}


@app.post("/api/v1/enrollments", tags=["Enrollments"], responses=_PROXIED_RESPONSES)
async def enroll(
    request: Request,
    payload: EnrollmentRequest,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_enrollment_upstream),
):
    """Enroll the authenticated student in a section (or join its waitlist)."""
    return await upstream.forward(request, "/enrollments", principal.sub)


@app.get("/api/v1/enrollments", tags=["Enrollments"], responses=_PROXIED_RESPONSES)
async def my_enrollments(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_enrollment_upstream),
):
    """List the authenticated student's enrollments."""
    return await upstream.forward(request, "/enrollments", principal.sub)


@app.post("/api/v1/drops", tags=["Enrollments"], responses=_PROXIED_RESPONSES)
async def drop(
    request: Request,
    payload: DropRequest,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_enrollment_upstream),
):
    """Drop a section or leave its waitlist."""
    return await upstream.forward(request, "/drops", principal.sub)


@app.get("/api/v1/courses", tags=["Catalog"], responses=_PROXIED_RESPONSES)
async def list_courses(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_catalog_upstream),
):
    """List courses (query filters are passed through to the Catalog Service)."""
    return await upstream.forward(request, "/courses", principal.sub)


@app.get("/api/v1/courses/{code}", tags=["Catalog"], responses=_PROXIED_RESPONSES)
async def get_course(
    code: str,
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_catalog_upstream),
):
    """Retrieve one course with its prerequisite codes."""
    return await upstream.forward(
        request, f"/courses/{quote(code, safe='')}", principal.sub
    )


@app.get("/api/v1/sections", tags=["Catalog"], responses=_PROXIED_RESPONSES)
async def list_sections(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_catalog_upstream),
):
    """List sections (query filters are passed through to the Catalog Service)."""
    return await upstream.forward(request, "/sections", principal.sub)


@app.get("/api/v1/sections/{section_id}", tags=["Catalog"], responses=_PROXIED_RESPONSES)
async def get_section(
    section_id: int,
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_catalog_upstream),
):
    """Retrieve one section with its live capacity."""
    return await upstream.forward(request, f"/sections/{section_id}", principal.sub)


@app.get("/api/v1/students/me", tags=["Students"], responses=_PROXIED_RESPONSES)
async def my_profile(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_student_upstream),
):
    """Retrieve the authenticated student's own profile."""
    return await upstream.forward(
        request, f"/students/{quote(principal.sub, safe='')}", principal.sub
    )


@app.get(
    "/api/v1/students/me/eligibility", tags=["Students"], responses=_PROXIED_RESPONSES
)
async def my_eligibility(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_student_upstream),
):
    """Retrieve the authenticated student's registration eligibility."""
    return await upstream.forward(
        request,
        f"/students/{quote(principal.sub, safe='')}/eligibility",
        principal.sub,
    )


@app.get(
    "/api/v1/notifications", tags=["Notifications"], responses=_PROXIED_RESPONSES
)
async def my_notifications(
    request: Request,
    principal: TokenPayload = Depends(get_current_principal),
    upstream: UpstreamService = Depends(get_notification_upstream),
):
    """Retrieve notifications and alerts for the authenticated student."""
    return await upstream.forward(
        request,
        f"/notifications/{quote(principal.sub, safe='')}",
        principal.sub,
    )
