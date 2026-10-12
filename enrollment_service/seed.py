"""Reset enrollment.db: uv run python -m enrollment_service.seed"""
import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from enrollment_service.database import engine
from enrollment_service.models import Base


async def reset_enrollment_data(bind: AsyncEngine = engine) -> None:
    """Drop and recreate all enrollment tables."""
    async with bind.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def main() -> None:
    """CLI runner for database reset."""
    print("=" * 60)
    print("  Resetting Enrollment Database (enrollment.db)...")
    print("=" * 60)

    await reset_enrollment_data()
    await engine.dispose()

    print("[OK] Recreated tables: enrollments, audit_log (empty).")
    print("-" * 60)
    print("  Note: seat counts live in catalog.db. Reseed it too:")
    print("  uv run python -m catalog_service.seed")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
