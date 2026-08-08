from datetime import datetime, timedelta

from app.models import (
    DEFAULT_LEVEL_HOURS, HourSubmission, LevelRequirement, StudentLevel, SubmissionStatus,
)
from app.services.app_settings import set_season_start
from app.services.requirements import (
    resolve_required_hours, season_required_opportunities, season_total_hours,
    level_requirements_map,
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


async def test_season_required_opportunities_none_flagged(db, make_opportunity, make_shift):
    opp = await make_opportunity()
    await make_shift(opp.id)
    assert await season_required_opportunities(db, None) == []


async def test_season_required_opportunities_excludes_shiftless(db, make_opportunity):
    """A required opportunity with no shifts yet isn't 'live' -- the inner join to
    Shift naturally drops it."""
    await make_opportunity(name="Bag Night", is_required=True)
    assert await season_required_opportunities(db, None) == []


async def test_season_required_opportunities_included_with_live_shift(db, make_opportunity, make_shift):
    opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(opp.id)
    result = await season_required_opportunities(db, None)
    assert [o.name for o in result] == ["Bag Night"]


async def test_season_required_opportunities_respects_cutoff(db, make_opportunity, make_shift):
    """A required opportunity whose only shift predates the season-start cutoff is
    excluded -- this is what protects a new student from being dinged for an event
    that happened before they joined."""
    opp = await make_opportunity(name="Old Fundraiser", is_required=True)
    await make_shift(opp.id, start_in_hours=-24 * 40)  # 40 days ago
    since = datetime.utcnow() - timedelta(days=7)
    assert await season_required_opportunities(db, since) == []

    # A shift after the cutoff makes it live again.
    await make_shift(opp.id, start_in_hours=24)
    result = await season_required_opportunities(db, since)
    assert [o.name for o in result] == ["Old Fundraiser"]


async def test_season_required_opportunities_no_cutoff_counts_everything(db, make_opportunity, make_shift):
    opp = await make_opportunity(name="Old Fundraiser", is_required=True)
    await make_shift(opp.id, start_in_hours=-24 * 400)
    result = await season_required_opportunities(db, None)
    assert [o.name for o in result] == ["Old Fundraiser"]


async def test_season_required_opportunities_ignores_archived_flag(db, make_opportunity, make_shift):
    """A required opportunity auto-archived after its shift ended must still count --
    otherwise the requirement would silently vanish right after the event."""
    opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(opp.id, start_in_hours=-5, length_hours=1)
    opp.is_active = False
    opp.archived_at = datetime.utcnow()
    await db.commit()

    result = await season_required_opportunities(db, None)
    assert [o.name for o in result] == ["Bag Night"]


async def test_season_required_opportunities_excludes_continuous(db, make_opportunity, make_shift):
    """is_required on a continuous opportunity is inconsistent data (the admin routes
    never allow it), but the query itself guards against it too, not just the join."""
    opp = await make_opportunity(name="CAD Subteam", is_required=True, is_continuous=True)
    await make_shift(opp.id)
    assert await season_required_opportunities(db, None) == []
