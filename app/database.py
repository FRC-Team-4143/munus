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

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Safe migration: add reviewer_mentor_id to opportunities and shifts if missing.
        await conn.run_sync(_add_reviewer_columns)

    await _seed_level_requirements()


def _add_reviewer_columns(conn) -> None:
    """Add reviewer_mentor_id to opportunities and shifts if not already present."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    for table in ("opportunities", "shifts"):
        columns = [c["name"] for c in inspector.get_columns(table)]
        if "reviewer_mentor_id" not in columns:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN reviewer_mentor_id INTEGER "
                f"REFERENCES mentors(id)"
            ))


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
