"""
common/auth.py
JWT creation and validation utilities for Centralized Gateway Authentication.
"""
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-student-registration-jwt-key-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24 hours


def create_access_token(
    subject: str,
    role: str = "STUDENT",
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    Mints a cryptographically signed JSON Web Token (HS256).
    Used by:
      - API Gateway /auth/login for real student logins
      - Automated test fixtures for zero-bypass concurrent load testing
    """
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(UTC),
    }

    if extra_claims:
        payload.update(extra_claims)

    encoded_jwt = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any] | None:
    """
    Decodes and verifies a JWT token's signature and expiration time.
    Returns payload dictionary if valid, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None
