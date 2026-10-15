"""Environment-driven settings for the API Gateway."""
import os

STUDENT_SERVICE_URL = os.getenv("STUDENT_SERVICE_URL", "http://localhost:8001")
CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002")
ENROLLMENT_SERVICE_URL = os.getenv("ENROLLMENT_SERVICE_URL", "http://localhost:8003")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8004")

HTTP_TIMEOUT_SECONDS = float(os.getenv("GATEWAY_HTTP_TIMEOUT_SECONDS", "15.0"))

# Per-student bucket for authenticated requests.
RATE_LIMIT_CAPACITY = int(os.getenv("GATEWAY_RATE_LIMIT_CAPACITY", "10"))
RATE_LIMIT_REFILL_PER_SECOND = float(
    os.getenv("GATEWAY_RATE_LIMIT_REFILL_PER_SECOND", "5")
)

# Per-IP bucket for login attempts (stricter: slows password guessing).
LOGIN_RATE_LIMIT_CAPACITY = int(os.getenv("GATEWAY_LOGIN_RATE_LIMIT_CAPACITY", "5"))
LOGIN_RATE_LIMIT_REFILL_PER_SECOND = float(
    os.getenv("GATEWAY_LOGIN_RATE_LIMIT_REFILL_PER_SECOND", "0.5")
)
