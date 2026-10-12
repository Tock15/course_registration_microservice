"""
api_gateway/auth.py
Centralized authentication: POST /api/v1/auth/login and the bearer-token dependency.
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from api_gateway.config import STUDENT_SERVICE_URL
from api_gateway.rate_limiter import TokenBucketRateLimiter
from common.auth import create_access_token, decode_access_token
from common.schemas import LoginRequest, TokenPayload, TokenResponse, UserRole

bearer_scheme = HTTPBearer(
    auto_error=False, description="JWT issued by POST /api/v1/auth/login"
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> TokenPayload:
    """Verify the bearer JWT (signature + expiry) and return its claims."""
    if credentials is None:
        raise _unauthorized("Missing bearer token.")

    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise _unauthorized("Invalid or expired token.")

    try:
        principal = TokenPayload.model_validate(claims)
    except ValidationError as exc:
        raise _unauthorized("Invalid or expired token.") from exc
    if not principal.sub.strip():
        raise _unauthorized("Invalid or expired token.")
    return principal


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_login_limiter(request: Request) -> TokenBucketRateLimiter:
    return request.app.state.login_limiter


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        401: {"description": "Invalid student ID or password"},
        429: {"description": "Too many login attempts"},
        503: {"description": "Student service unavailable"},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    http: httpx.AsyncClient = Depends(get_http_client),
    limiter: TokenBucketRateLimiter = Depends(get_login_limiter),
):
    """Verify credentials with the Student Service and issue a signed JWT."""
    client_host = request.client.host if request.client else "unknown"
    limiter.check(f"ip:{client_host}")

    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Student service is unavailable; please retry.",
    )
    try:
        res = await http.post(
            f"{STUDENT_SERVICE_URL.rstrip('/')}/students/verify-credentials",
            json=payload.model_dump(),
        )
    except httpx.HTTPError as exc:
        raise unavailable from exc
    if res.status_code != status.HTTP_200_OK:
        raise unavailable

    verdict = res.json()
    if not verdict.get("valid"):
        raise _unauthorized("Invalid student ID or password.")

    student_id = verdict["student_id"]
    role = UserRole(verdict.get("role", UserRole.STUDENT.value))
    return TokenResponse(
        access_token=create_access_token(student_id, role=role.value),
        role=role,
        student_id=student_id,
    )
