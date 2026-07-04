from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables and seed initial data."""
    from app import models  # noqa: F401 — imported for side-effect (table registration)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Future schema changes: add a `def _migration(conn)` guarded by inspect(conn)
        # and call it here, mirroring Tempus's hand-rolled migration pattern.

    await _seed_level_requirements()


async def _seed_level_requirements() -> None:
    """Insert the default per-level required hours if the table is empty."""
    from sqlalchemy import select
    from app.models import DEFAULT_LEVEL_HOURS, LevelRequirement

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(LevelRequirement.level))).scalars().all()
        existing_levels = set(existing)
        for level, hours in DEFAULT_LEVEL_HOURS.items():
            if level not in existing_levels:
                session.add(LevelRequirement(level=level, required_hours=hours))
        await session.commit()
