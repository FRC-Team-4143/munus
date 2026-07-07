"""Tests for the editable admin Settings page: .env writeback + validation.

The scheduler isn't started under the test transport, so the handler's
``reschedule_all`` call is a safe no-op (``app.state.scheduler`` is absent).
"""
import pytest

from app.config import settings
from app.routers import admin
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie

_MUTABLE = [
    "slack_announce_channel", "timezone", "reminder_lead_hours",
    "auto_reject_days", "backup_day", "backup_time", "backup_keep", "updates_enabled",
]


async def _login(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie())


def _form(**overrides):
    """A complete settings form pre-filled from the current singleton."""
    form = {
        "season_start": "",
        "slack_announce_channel": settings.slack_announce_channel,
        "timezone": settings.timezone,
        "reminder_lead_hours": settings.reminder_lead_hours,
        "auto_reject_days": settings.auto_reject_days,
        "backup_day": settings.backup_day,
        "backup_time": settings.backup_time,
        "backup_keep": settings.backup_keep,
    }
    form.update(overrides)
    return form


@pytest.fixture
def restore_settings():
    snapshot = {k: getattr(settings, k) for k in _MUTABLE}
    yield
    for k, v in snapshot.items():
        setattr(settings, k, v)


async def test_settings_post_writes_env_and_updates_singleton(
    client, tmp_path, monkeypatch, restore_settings
):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    await _login(client)

    # Known baseline so each POSTed value is an actual change.
    settings.timezone = "America/New_York"
    settings.backup_day = "sun"
    settings.backup_time = "23:30"
    settings.reminder_lead_hours = 24
    settings.auto_reject_days = 7
    settings.slack_announce_channel = ""

    resp = await client.post("/admin/settings", data=_form(
        timezone="America/Denver",
        backup_day="fri",
        backup_time="02:15",
        reminder_lead_hours="12",
        auto_reject_days="3",
        slack_announce_channel="C0ANNOUNCE",
        updates_enabled="true",
    ), follow_redirects=False)

    assert resp.status_code == 303
    assert "error" not in resp.headers.get("location", "")

    # Live singleton updated immediately.
    assert settings.timezone == "America/Denver"
    assert settings.backup_day == "fri"
    assert settings.backup_time == "02:15"
    assert settings.reminder_lead_hours == 12
    assert settings.auto_reject_days == 3
    assert settings.slack_announce_channel == "C0ANNOUNCE"

    # Persisted to .env for the next restart.
    written = env_file.read_text()
    assert "TIMEZONE=America/Denver" in written
    assert "BACKUP_DAY=fri" in written
    assert "BACKUP_TIME=02:15" in written
    assert "REMINDER_LEAD_HOURS=12" in written
    assert "AUTO_REJECT_DAYS=3" in written
    assert "SLACK_ANNOUNCE_CHANNEL=C0ANNOUNCE" in written


async def test_settings_post_rejects_bad_timezone(
    client, tmp_path, monkeypatch, restore_settings
):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    await _login(client)
    original_tz = settings.timezone

    resp = await client.post(
        "/admin/settings", data=_form(timezone="Not/AZone"), follow_redirects=False
    )

    assert resp.status_code == 303
    assert "error=" in resp.headers.get("location", "")
    assert settings.timezone == original_tz
    if env_file.exists():
        assert "Not/AZone" not in env_file.read_text()
