"""
api_gateway/main.py
API Gateway & Protection Proxy (Port 8000).

Responsibilities for Teammate:
  - Centralized Authentication (POST /api/v1/auth/login) via auth.py & common.auth
  - Reverse Proxy routing to downstream microservices (:8001 - :8004)
  - Protection Proxy: Anti-spam token bucket rate limiting (rate_limiter.py)
  - Security boundary: Inject verified X-Student-Id header downstream
"""
from fastapi import FastAPI

app = FastAPI(
    title="API Gateway & Security Proxy",
    description="Centralized ingress reverse proxy, rate limiter, and JWT authentication checkpoint.",
    version="0.1.0",
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
