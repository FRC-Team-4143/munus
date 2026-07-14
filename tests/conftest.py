"""
Shared pytest fixtures.

Every test runs against a fresh in-memory SQLite database. We use a StaticPool so the
single in-memory connection is shared across the session (in-memory DBs are otherwise
per-connection and would appear empty).
"""
from datetime import datetime, timedelta
from typing import Optional

import pytest
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
    """Factory: make_student(name=..., code=..., level=..., slack=..., active=True).

    `code` is stored as `member_code` — the Legion sync key, and what a test's
    `make_sso_cookie(member_code=...)` must match for `_current_student` to resolve it.
    """
    async def _make(
        name: str = "Ada Lovelace",
        code: str = "ada00001",
        level: Optional[StudentLevel] = StudentLevel.team_4143,
        team_number: Optional[int] = None,
        grade: Optional[str] = None,
        slack: str | None = None,
        is_active: bool = True,
    ) -> Student:
        s = Student(
            name=name, member_code=code, level=level, team_number=team_number,
            grade=grade, slack_user_id=slack, is_active=is_active,
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s
    return _make


@pytest_asyncio.fixture
async def make_mentor(db):
    async def _make(
        name: str = "Coach Ray", slack: str | None = "U0MENTOR",
        code: str | None = None, is_active: bool = True,
    ) -> Mentor:
        m = Mentor(name=name, slack_user_id=slack, member_code=code, is_active=is_active)
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
async def client(session_factory, monkeypatch):
    """An httpx AsyncClient wired to the app with get_db overridden to the test DB.

    Also redirects app.database.AsyncSessionLocal to the same test engine:
    services/submissions.py's notify_reviewer/notify_student_of_review background
    tasks open their own session via a function-local `from app.database import
    AsyncSessionLocal` instead of the request-injected get_db, since they run after
    the request's own session is already closed. Without this, those code paths
    would silently hit the real on-disk munus.db instead of the test's in-memory
    DB — passing only by accident if a stray munus.db with tables already exists
    locally, and reliably failing with "no such table" on a clean checkout (e.g. CI)."""
    import httpx
    import app.database as database_module
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def make_sso_cookie(
    groups=("munus-admin",), *, name="Test Admin", username="test.admin",
    role="mentor", member_code="test0001", team_number=4143, slack_user_id=None,
):
    """Mint a valid `mw_sso` cookie value for tests, mirroring Legion's `make_sso_token`.
    Uses the app's own `sso_secret`, so `read_sso_token` verifies it. `role="student"` +
    a `member_code` matching a `make_student(code=...)` row is what the portal resolves
    `_current_student` from."""
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings

    signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")
    return signer.dumps({
        "member_code": member_code,
        "username": username,
        "name": name,
        "role": role,
        "team_number": team_number,
        "groups": list(groups),
        "slack_user_id": slack_user_id,
    })


@pytest_asyncio.fixture
async def authed_client(client):
    """An httpx client carrying a valid `mw_sso` cookie in the `munus-admin` group."""
    from app.services.sso import SSO_COOKIE

    client.cookies.set(SSO_COOKIE, make_sso_cookie())
    return client


@pytest.fixture
def hush_slack(monkeypatch):
    """Silence outbound Slack calls (webhooks / DMs / reviewer notify). notify_reviewer
    and notify_student_of_review open their own real AsyncSessionLocal (bypassing the
    test DB override), so any route that schedules them as a background task needs
    this fixture even if the notification itself would've no-op'd (e.g. no slack_user_id)."""
    import app.routers.slack as slackmod
    import app.services.submissions as subs
    import slack_sdk.webhook.async_client as whmod

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(subs, "notify_reviewer", _noop)
    monkeypatch.setattr(subs, "notify_student_of_review", _noop)
    monkeypatch.setattr(slackmod, "send_dm", _noop)

    class _FakeWebhook:
        def __init__(self, *a, **k):
            pass

        async def send(self, *a, **k):
            return None

    monkeypatch.setattr(whmod, "AsyncWebhookClient", _FakeWebhook)
