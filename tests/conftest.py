"""
Shared pytest fixtures.

Every test runs against a fresh in-memory SQLite database. We use a StaticPool so the
single in-memory connection is shared across the session (in-memory DBs are otherwise
per-connection and would appear empty).
"""
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Mentor, Opportunity, Shift, Student, StudentLevel,
)


@pytest_asyncio.fixture
async def engine():
    """A fresh in-memory database engine with all tables created."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    """A database session for direct service-layer tests."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def make_student(db):
    """Factory: make_student(name=..., code=..., level=..., slack=..., active=True)."""
    async def _make(
        name: str = "Ada Lovelace",
        code: str = "ada00001",
        level: StudentLevel = StudentLevel.team_4143,
        slack: str | None = None,
        is_active: bool = True,
    ) -> Student:
        s = Student(name=name, student_code=code, level=level, slack_user_id=slack, is_active=is_active)
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s
    return _make


@pytest_asyncio.fixture
async def make_mentor(db):
    async def _make(name: str = "Coach Ray", slack: str | None = "U0MENTOR") -> Mentor:
        m = Mentor(name=name, slack_user_id=slack)
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m
    return _make


@pytest_asyncio.fixture
async def make_opportunity(db):
    async def _make(name: str = "Food Drive", **kw) -> Opportunity:
        o = Opportunity(name=name, **kw)
        db.add(o)
        await db.commit()
        await db.refresh(o)
        return o
    return _make


@pytest_asyncio.fixture
async def make_shift(db):
    async def _make(opportunity_id: int, capacity: int = 0, start_in_hours: float = 24, length_hours: float = 3) -> Shift:
        start = datetime.utcnow() + timedelta(hours=start_in_hours)
        sh = Shift(
            opportunity_id=opportunity_id,
            start_time=start,
            end_time=start + timedelta(hours=length_hours),
            capacity=capacity,
        )
        db.add(sh)
        await db.commit()
        await db.refresh(sh)
        return sh
    return _make


@pytest_asyncio.fixture
async def client(session_factory):
    """An httpx AsyncClient wired to the app with get_db overridden to the test DB."""
    import httpx
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
