"""ORM models for enrollments and the audit log (naive UTC timestamps)."""
import json
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from enrollment_service.state_machine import ACTIVE_STATUSES


def utcnow() -> datetime:
    """Current time as naive UTC, the storage convention for enrollment.db."""
    return datetime.now(UTC).replace(tzinfo=None)


_ACTIVE_STATUS_SQL = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)


class Base(DeclarativeBase):
    pass


class Enrollment(Base):
    """Registration record; a non-null seat_key means Catalog holds a seat."""

    __tablename__ = "enrollments"
    __table_args__ = (
        Index(
            "uq_enrollments_active_student_section",
            "student_id",
            "section_id",
            unique=True,
            sqlite_where=text(f"status IN ({_ACTIVE_STATUS_SQL})"),
        ),
        Index(
            "ix_enrollments_waitlist_order",
            "section_id",
            "status",
            "waitlisted_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    section_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_code: Mapped[str] = mapped_column(String(20), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    schedule_days_raw: Mapped[str] = mapped_column(
        "schedule_days", String(100), default="[]"
    )
    start_time: Mapped[str] = mapped_column(String(10), nullable=False)
    end_time: Mapped[str] = mapped_column(String(10), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    seat_key: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    waitlisted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    @property
    def schedule_days(self) -> list[str]:
        """Expose days as a parsed list of strings, e.g. ['MON', 'WED']."""
        try:
            parsed = json.loads(self.schedule_days_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(d) for d in parsed] if isinstance(parsed, list) else []

    @schedule_days.setter
    def schedule_days(self, days: list[str]) -> None:
        self.schedule_days_raw = json.dumps([d.upper() for d in days])


class AuditLog(Base):
    """Append-only trail of every attempt and state transition."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enrollment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    student_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    section_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
