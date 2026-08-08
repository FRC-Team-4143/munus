"""
Season requirement resolution and season-total calculation.

The required hours for a student are driven entirely by their level. The values are
stored in the `level_requirements` table (admin-editable) and fall back to
DEFAULT_LEVEL_HOURS if a row is missing.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DEFAULT_LEVEL_HOURS, HourSubmission, LevelRequirement, Opportunity, Shift,
    StudentLevel, SubmissionStatus,
)
from app.services.app_settings import season_start_utc


async def resolve_required_hours(db: AsyncSession, level: Optional[StudentLevel]) -> float:
    """Return the season required hours for a student level. A student with no level
    (alumni/no-grade — see `derive_level`) has no season requirement at all."""
    if level is None:
        return 0.0
    row = (
        await db.execute(
            select(LevelRequirement).where(LevelRequirement.level == level)
        )
    ).scalars().first()
    if row is not None:
        return row.required_hours
    return DEFAULT_LEVEL_HOURS.get(level, 0.0)


async def season_total_hours(db: AsyncSession, student_id: int) -> float:
    """Sum of a student's *approved* submission hours since the season start cutoff."""
    since = await season_start_utc(db)
    q = (
        select(func.coalesce(func.sum(HourSubmission.hours), 0.0))
        .where(
            HourSubmission.student_id == student_id,
            HourSubmission.status == SubmissionStatus.approved,
        )
    )
    if since is not None:
        q = q.where(HourSubmission.submitted_at >= since)
    result = await db.execute(q)
    return float(result.scalar() or 0.0)


async def level_requirements_map(db: AsyncSession) -> dict[StudentLevel, float]:
    """All level → required-hours, filling in defaults for any missing rows."""
    rows = (await db.execute(select(LevelRequirement))).scalars().all()
    by_level: dict[StudentLevel, float] = {r.level: r.required_hours for r in rows}
    for level, hours in DEFAULT_LEVEL_HOURS.items():
        by_level.setdefault(level, hours)
    return by_level


async def season_required_opportunities(
    db: AsyncSession, since: Optional[datetime]
) -> list[Opportunity]:
    """Shift-based opportunities flagged `is_required` that are 'live' for the current
    season — i.e. have at least one shift on/after the season-start cutoff (`since`,
    from `season_start_utc`), or any shift at all if there's no cutoff configured.
    Mirrors the same cutoff `season_total_hours` uses for approved hours, so a new
    student isn't dinged for a required opportunity that predates them.

    Deliberately does NOT filter on `Opportunity.is_active`: the nightly auto-archive
    job (see CLAUDE.md) flips a shift-based opportunity inactive once its last shift
    ends, but a required opportunity that already happened must still count against
    students who skipped it — otherwise the requirement would silently vanish right
    after the event. `is_continuous.is_(False)` is belt-and-suspenders (the admin
    routes never let a continuous opportunity be marked required in the first place).
    """
    q = (
        select(Opportunity)
        .join(Shift, Shift.opportunity_id == Opportunity.id)
        .where(Opportunity.is_required.is_(True), Opportunity.is_continuous.is_(False))
        .distinct()
        .order_by(Opportunity.name)
    )
    if since is not None:
        q = q.where(Shift.start_time >= since)
    return list((await db.execute(q)).scalars().all())


def derive_level(grade: Optional[str], team_number: Optional[int]) -> Optional[StudentLevel]:
    """Map a student's synced Legion `grade` + `team_number` to a requirement pool.

        junior_high / freshman grade      -> Freshman        (any team)
        sophomore + team 4423              -> 4423 Student
        anything else (any other grade/team combo, including no team) -> 4143 Student
        alumni or no grade (never happens for an active student; mentors have no
        grade at all) -> no level, excluded from level-based reporting

    Applied by `services/legion_sync.py` on every sync and materialized onto
    `Student.level` — see that module for why this isn't just derived at read time.
    """
    if grade in (None, "alumni"):
        return None
    if grade in ("junior_high", "freshman"):
        return StudentLevel.freshman
    if grade == "sophomore" and team_number == 4423:
        return StudentLevel.team_4423
    return StudentLevel.team_4143
