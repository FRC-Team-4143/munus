"""Legion roster sync (services/legion_sync.py) — upsert by member_code, back-link
legacy rows, and the level-derivation rule (services/requirements.derive_level)."""
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Mentor, Student, StudentLevel
from app.services import legion_sync
from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting
from app.services.requirements import derive_level

STUDENT = {
    "member_code": "stu00001", "name": "Ada Student", "role": "student",
    "team_number": 4143, "grade": "junior", "slack_user_id": "USTU", "is_active": True,
}
MENTOR = {
    "member_code": "men00001", "name": "Grace Mentor", "role": "mentor",
    "team_number": 4143, "grade": None, "slack_user_id": "UMEN", "is_active": True,
}


@pytest.fixture
def legion_api(monkeypatch):
    """Configure Legion + stub the HTTP layer. Override `members` per-test if needed."""
    monkeypatch.setattr(settings, "legion_base_url", "http://legion.test")
    monkeypatch.setattr(settings, "legion_api_key", "key")

    state = {"members": [STUDENT, MENTOR]}

    async def fake_get(client, path, **params):
        if path == "/api/members":
            return {"members": state["members"]}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(legion_sync, "_get", fake_get)
    return state


async def test_sync_upserts_roster(db, legion_api):
    summary = await legion_sync.sync_roster(db)
    assert "1 students" in summary and "1 mentors" in summary

    student = (await db.execute(select(Student).where(Student.member_code == "stu00001"))).scalar_one()
    assert student.name == "Ada Student"
    assert student.team_number == 4143
    assert student.grade == "junior"
    assert student.level == StudentLevel.team_4143

    mentor = (await db.execute(select(Mentor).where(Mentor.member_code == "men00001"))).scalar_one()
    assert mentor.name == "Grace Mentor"
    assert mentor.slack_user_id == "UMEN"


async def test_sync_backlinks_legacy_row_by_slack_id(db, legion_api):
    # A pre-existing student created before the cutover (no member_code), same slack id.
    db.add(Student(name="Ada Student", slack_user_id="USTU", level=StudentLevel.freshman))
    await db.commit()

    await legion_sync.sync_roster(db)

    students = (await db.execute(select(Student).where(Student.slack_user_id == "USTU"))).scalars().all()
    assert len(students) == 1  # linked, not duplicated
    assert students[0].member_code == "stu00001"


async def test_sync_backlinks_legacy_row_by_name_when_no_slack_id(db, legion_api):
    db.add(Student(name="Ada Student", level=StudentLevel.freshman))
    await db.commit()

    await legion_sync.sync_roster(db)

    students = (await db.execute(select(Student).where(Student.name == "Ada Student"))).scalars().all()
    assert len(students) == 1
    assert students[0].member_code == "stu00001"


async def test_sync_deactivates_archived_member(db, legion_api):
    await legion_sync.sync_roster(db)
    legion_api["members"] = [dict(STUDENT, is_active=False)]
    await legion_sync.sync_roster(db, full=True)

    student = (await db.execute(select(Student).where(Student.member_code == "stu00001"))).scalar_one()
    assert student.is_active is False
    assert student.archived_at is not None


async def test_sync_advances_watermark(db, legion_api):
    assert await get_setting(db, LEGION_LAST_SYNCED_KEY) is None
    await legion_sync.sync_roster(db)
    assert await get_setting(db, LEGION_LAST_SYNCED_KEY) is not None


async def test_sync_requires_configuration(db, monkeypatch):
    monkeypatch.setattr(settings, "legion_base_url", "")
    monkeypatch.setattr(settings, "legion_api_key", "")
    with pytest.raises(legion_sync.LegionSyncError):
        await legion_sync.sync_roster(db)


# ── derive_level: the confirmed grade+team -> requirement pool rule ────────────────

@pytest.mark.parametrize("grade,team_number,expected", [
    ("junior_high", 4143, StudentLevel.freshman),
    ("junior_high", 4423, StudentLevel.freshman),
    ("junior_high", None, StudentLevel.freshman),
    ("freshman", 4143, StudentLevel.freshman),
    ("freshman", 4423, StudentLevel.freshman),
    ("sophomore", 4143, StudentLevel.team_4143),
    ("sophomore", 4423, StudentLevel.team_4423),
    ("sophomore", None, StudentLevel.team_4143),
    ("junior", 4143, StudentLevel.team_4143),
    ("junior", 4423, StudentLevel.team_4143),
    ("senior", 4143, StudentLevel.team_4143),
    ("senior", 4423, StudentLevel.team_4143),
    ("senior", None, StudentLevel.team_4143),
    ("alumni", 4143, None),
    (None, 4143, None),
    (None, None, None),
])
def test_derive_level(grade, team_number, expected):
    assert derive_level(grade, team_number) == expected


async def test_sync_sets_no_level_for_alumni_student(db, legion_api):
    legion_api["members"] = [dict(STUDENT, grade="alumni")]
    await legion_sync.sync_roster(db)
    student = (await db.execute(select(Student).where(Student.member_code == "stu00001"))).scalar_one()
    assert student.level is None


async def test_sync_does_not_touch_mentor_level(db, legion_api):
    await legion_sync.sync_roster(db)
    assert (await db.scalar(select(func.count()).select_from(Mentor))) == 1
