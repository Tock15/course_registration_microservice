"""
enrollment_service/config.py
Environment-driven settings for the Enrollment Service.
"""
import os
from pathlib import Path

DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "enrollment.db"
DATABASE_URL = os.getenv(
    "ENROLLMENT_DATABASE_URL",
    f"sqlite+aiosqlite:///{DEFAULT_DB_FILE.as_posix()}",
)

STUDENT_SERVICE_URL = os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")
CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")
NOTIFICATION_SERVICE_URL = os.getenv(
    "NOTIFICATION_SERVICE_URL", "http://localhost:8004"
)

HTTP_TIMEOUT_SECONDS = float(os.getenv("ENROLLMENT_HTTP_TIMEOUT_SECONDS", "3.0"))

RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("ENROLLMENT_RECONCILE_INTERVAL_SECONDS", "15")
)
STAGED_STALE_SECONDS = float(os.getenv("ENROLLMENT_STAGED_STALE_SECONDS", "60"))

SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("ENROLLMENT_SQLITE_BUSY_TIMEOUT_MS", "5000"))
