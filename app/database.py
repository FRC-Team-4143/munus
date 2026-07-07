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
        # Drops run BEFORE create_all so SQLAlchemy sees no `students` table and builds
        # a fresh one with the current schema.
        await conn.run_sync(_migration_drop_students_if_legacy_schema)
        await conn.run_sync(Base.metadata.create_all)
        # Additive column migrations run after create_all (safe on both fresh + existing).
        await conn.run_sync(_add_reviewer_columns)
        await conn.run_sync(_add_mentor_member_code_column)

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


def _add_mentor_member_code_column(conn) -> None:
    """Add `member_code` (Legion's sync key) to mentors if not already present, with its
    unique index. No-op on a fresh schema, which already has both from create_all()."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    if "mentors" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("mentors")]
    if "member_code" not in columns:
        conn.execute(text("ALTER TABLE mentors ADD COLUMN member_code VARCHAR(8)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_mentors_member_code ON mentors (member_code)"
        ))


def _migration_drop_students_if_legacy_schema(conn) -> None:
    """The Legion rework makes `Student.level` nullable (alumni/no-grade students have no
    requirement pool — see services/requirements.derive_level) and drops `student_code`
    in favor of `member_code`. No production data predates this rework, so rather than
    migrate it in place, just drop the old table — create_all() rebuilds it fresh right
    after, and a Legion sync repopulates it. No-op on a fresh database (no `students`
    table yet) or one already on the current schema (`level` is already nullable)."""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    if "students" not in inspector.get_table_names():
        return
    cols = {c["name"]: c for c in inspector.get_columns("students")}
    if "level" not in cols or cols["level"]["nullable"]:
        return  # already on the current schema — nothing to drop
    conn.execute(text("DROP TABLE students"))


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
