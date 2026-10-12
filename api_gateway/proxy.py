"""
api_gateway/proxy.py
Protection Proxy Pattern (GoF) in front of the downstream microservices.

  Subject      -> UpstreamService   (common interface)
  RealSubject  -> HttpUpstream      (actually forwards the HTTP request)
  Proxy        -> ProtectionProxy   (access control, then delegates)
"""
from abc import ABC, abstractmethod

import httpx
from fastapi import HTTPException, Request, Response, status

from api_gateway.rate_limiter import TokenBucketRateLimiter

STUDENT_ID_HEADER = "X-Student-Id"

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Never forwarded: the JWT stays at the gateway, and a client-supplied
# X-Student-Id would let a caller impersonate another student.
_DROPPED_REQUEST_HEADERS = _HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "accept-encoding",
    "authorization",
    STUDENT_ID_HEADER.lower(),
}
# httpx already decoded the body, so the upstream encoding/length no longer apply.
_DROPPED_RESPONSE_HEADERS = _HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}


class UpstreamService(ABC):
    """Subject: a downstream microservice reachable on behalf of a student."""

    @abstractmethod
    async def forward(self, request: Request, path: str, student_id: str) -> Response:
        """Send `request` to `path` on the service as `student_id`."""


class HttpUpstream(UpstreamService):
    """RealSubject: forwards the request over HTTP and relays the response as-is."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, service_name: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._service_name = service_name

    async def forward(self, request: Request, path: str, student_id: str) -> Response:
        url = f"{self._base_url}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _DROPPED_REQUEST_HEADERS
        }
        headers[STUDENT_ID_HEADER] = student_id

        try:
            upstream = await self._http.request(
                request.method, url, content=await request.body(), headers=headers
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"{self._service_name} timed out.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{self._service_name} is unreachable.",
            ) from exc

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() not in _DROPPED_RESPONSE_HEADERS
            },
        )


class ProtectionProxy(UpstreamService):
    """Proxy: rate-limits the authenticated student before reaching the real service."""

    def __init__(self, real: UpstreamService, limiter: TokenBucketRateLimiter) -> None:
        self._real = real
        self._limiter = limiter

    async def forward(self, request: Request, path: str, student_id: str) -> Response:
        self._limiter.check(f"student:{student_id}")
        return await self._real.forward(request, path, student_id)
