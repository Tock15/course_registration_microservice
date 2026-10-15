"""
notification_service/seed.py
Database reset and sample alert seeding fixtures for Notification Service.
"""
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from notification_service.database import engine
from notification_service.models import Base, NotificationLog


async def reset_notification_data(
    session: AsyncSession | None = None, bind: AsyncEngine = engine
) -> dict[str, int]:
    """Reset the notifications database to a clean, empty state."""
    if session is not None:
        result = await session.execute(delete(NotificationLog))
        await session.commit()
        return {"notifications_cleared": result.rowcount or 0}

    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return {"notifications_cleared": 0}


async def seed_sample_notifications(session: AsyncSession) -> int:
    """Optional seed fixture adding sample notifications for initial testing."""
    sample_logs = [
        NotificationLog(
            event_id="seed-event-001",
            recipient_id="STU_001",
            event_type="ENROLLMENT_CONFIRMED",
            subject="Registration Confirmed: CS101",
            message="You have been successfully enrolled in CS101 (Section 1) for 3 credits.",
            channel="IN_APP",
            status="DELIVERED",
        ),
        NotificationLog(
            event_id="seed-event-002",
            recipient_id="STU_001",
            event_type="WAITLIST_JOINED",
            subject="Waitlist Confirmation: CS301",
            message="Section 1 of CS301 is full. You joined the waitlist at position #1.",
            channel="IN_APP",
            status="DELIVERED",
        ),
    ]
    for log in sample_logs:
        session.add(log)
    await session.commit()
    return len(sample_logs)


async def main() -> None:
    """CLI runner for resetting the notifications database."""
    print("=" * 60)
    print("  Resetting Notification Database (notifications.db)...")
    print("=" * 60)
    await reset_notification_data()
    print("  Done. Notifications database reset.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
