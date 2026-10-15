"""
notification_service/models.py
SQLAlchemy declarative models for notification history and alert audit store.
"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.events import NotificationRecord


class Base(DeclarativeBase):
    pass


class NotificationLog(Base):
    """Immutable persistent log of notifications dispatched to students."""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    recipient_id: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False
    )  # student_id
    event_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="IN_APP", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="DELIVERED", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )

    def to_record(self) -> NotificationRecord:
        """Convert ORM model to shared Pydantic DTO."""
        return NotificationRecord(
            id=self.id,
            recipient_id=self.recipient_id,
            event_type=self.event_type,
            subject=self.subject,
            message=self.message,
            channel=self.channel,
            status=self.status,
            created_at=self.created_at,
        )
