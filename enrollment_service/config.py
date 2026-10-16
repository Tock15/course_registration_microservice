"""Environment-driven settings for the Enrollment Service."""
import os

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Configuration settings for Enrollment Service including PostgreSQL connection pooling."""

    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            os.getenv(
                "ENROLLMENT_DATABASE_URL",
                "postgresql+asyncpg://postgres:postgres@localhost:5432/enrollment_db",
            ),
        )
    )
    pool_size: int = Field(
        default_factory=lambda: int(os.getenv("ENROLLMENT_POOL_SIZE", "30"))
    )
    max_overflow: int = Field(
        default_factory=lambda: int(os.getenv("ENROLLMENT_MAX_OVERFLOW", "40"))
    )

    student_service_url: str = Field(
        default_factory=lambda: os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")
    )
    catalog_service_url: str = Field(
        default_factory=lambda: os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")
    )
    notification_service_url: str = Field(
        default_factory=lambda: os.getenv(
            "NOTIFICATION_SERVICE_URL", "http://localhost:8004"
        )
    )

    http_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("ENROLLMENT_HTTP_TIMEOUT_SECONDS", "10.0"))
    )
    reconcile_interval_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("ENROLLMENT_RECONCILE_INTERVAL_SECONDS", "15")
        )
    )
    staged_stale_seconds: float = Field(
        default_factory=lambda: float(os.getenv("ENROLLMENT_STAGED_STALE_SECONDS", "60"))
    )
    offer_ttl_hours: float = Field(
        default_factory=lambda: float(os.getenv("ENROLLMENT_OFFER_TTL_HOURS", "24"))
    )


settings = Settings()

# Backward-compatible module-level constants
DATABASE_URL = settings.database_url
STUDENT_SERVICE_URL = settings.student_service_url
CATALOG_SERVICE_URL = settings.catalog_service_url
NOTIFICATION_SERVICE_URL = settings.notification_service_url
HTTP_TIMEOUT_SECONDS = settings.http_timeout_seconds
RECONCILE_INTERVAL_SECONDS = settings.reconcile_interval_seconds
STAGED_STALE_SECONDS = settings.staged_stale_seconds
OFFER_TTL_HOURS = settings.offer_ttl_hours
