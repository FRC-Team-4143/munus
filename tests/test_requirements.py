from datetime import datetime, timedelta

from app.models import (
    DEFAULT_LEVEL_HOURS, HourSubmission, LevelRequirement, StudentLevel, SubmissionStatus,
)
from app.services.app_settings import set_season_start
from app.services.requirements import (
    resolve_required_hours, season_total_hours, level_requirements_map,
)


async def test_resolve_falls_back_to_defaults(db):
    for level, hours in DEFAULT_LEVEL_HOURS.items():
        assert await resolve_required_hours(db, level) == hours


async def test_resolve_uses_stored_override(db):
    db.add(LevelRequirement(level=StudentLevel.freshman, required_hours=8.0))
    await db.commit()
    assert await resolve_required_hours(db, StudentLevel.freshman) == 8.0


async def test_requirements_map_fills_defaults(db):
    db.add(LevelRequirement(level=StudentLevel.team_4143, required_hours=20.0))
    await db.commit()
    mapping = await level_requirements_map(db)
    assert mapping[StudentLevel.team_4143] == 20.0
    assert mapping[StudentLevel.freshman] == DEFAULT_LEVEL_HOURS[StudentLevel.freshman]


async def test_season_total_counts_only_approved(db, make_student):
    student = await make_student()
    db.add_all([
        HourSubmission(student_id=student.id, hours=3.0, status=SubmissionStatus.approved),
        HourSubmission(student_id=student.id, hours=2.0, status=SubmissionStatus.approved),
        HourSubmission(student_id=student.id, hours=5.0, status=SubmissionStatus.pending),
        HourSubmission(student_id=student.id, hours=4.0, status=SubmissionStatus.rejected),
    ])
    await db.commit()
    assert await season_total_hours(db, student.id) == 5.0


async def test_season_total_respects_cutoff(db, make_student):
    student = await make_student()
    old = HourSubmission(
        student_id=student.id, hours=6.0, status=SubmissionStatus.approved,
        submitted_at=datetime.utcnow() - timedelta(days=40),
    )
    recent = HourSubmission(
        student_id=student.id, hours=2.0, status=SubmissionStatus.approved,
        submitted_at=datetime.utcnow(),
    )
    db.add_all([old, recent])
    await db.commit()

    # No cutoff -> everything counts.
    assert await season_total_hours(db, student.id) == 8.0

    # Cutoff in the recent past -> only the recent submission counts.
    await set_season_start(db, (datetime.utcnow() - timedelta(days=7)).date())
    assert await season_total_hours(db, student.id) == 2.0
