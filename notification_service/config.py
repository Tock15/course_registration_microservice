"""
notification_service/config.py
Configuration settings for Notification Service.
"""
import os

DATABASE_URL = os.getenv(
    "NOTIFICATION_DATABASE_URL", "sqlite+aiosqlite:///notifications.db"
)
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
