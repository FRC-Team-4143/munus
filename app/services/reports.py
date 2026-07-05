"""
Student progress report — one row per student with approved / projected / required hours.

Munus requirements are a season total by level (not weekly like Tempus), so the report is
a roster progress table rather than a per-week grid. All aggregates are computed with a
handful of grouped queries (no per-student N+1).
"""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    HourSubmission, Shift, Signup, SignupStatus, Student, StudentLevel, SubmissionStatus,
)
from app.services.app_settings import season_start_utc
from app.services.opportunities import upcoming_signups_for_student
from app.services.requirements import (
    level_requirements_map, resolve_required_hours, season_total_hours,
)
from app.services.student_auth import magic_link
from app.utils import format_shift_range, now_utc, shift_length_hours


async def student_progress_report(
    db: AsyncSession,
    level: Optional[StudentLevel] = None,
    include_archived: bool = False,
) -> list[dict]:
    """One dict per student (sorted by name):
      {student, approved, projected, required, remaining, pct, pending_count,
       upcoming_count, met}

    `projected` is a forward-looking estimate that stays stable across a shift's lifecycle:
    approved hours + pending submissions (at their submitted value) + the scheduled length
    of any signed-up shift not yet logged. A shift keeps counting until it is approved
    (counted at its real hours) or rejected (dropped).
    """
    student_q = select(Student).order_by(Student.name)
    if not include_archived:
        student_q = student_q.where(Student.is_active.is_(True))
    if level is not None:
        student_q = student_q.where(Student.level == level)
    students = (await db.execute(student_q)).scalars().all()
    if not students:
        return []

    student_ids = [s.id for s in students]

    # Approved hours per student (respecting the season-start cutoff).
    since = await season_start_utc(db)
    approved_q = (
        select(HourSubmission.student_id, func.coalesce(func.sum(HourSubmission.hours), 0.0))
        .where(
            HourSubmission.student_id.in_(student_ids),
            HourSubmission.status == SubmissionStatus.approved,
        )
        .group_by(HourSubmission.student_id)
    )
    if since is not None:
        approved_q = approved_q.where(HourSubmission.submitted_at >= since)
    approved_by_student = {sid: float(hrs) for sid, hrs in (await db.execute(approved_q)).all()}

    # Pending submissions — count and summed hours per student (hours feed the projection).
    pending_q = (
        select(
            HourSubmission.student_id,
            func.count(),
            func.coalesce(func.sum(HourSubmission.hours), 0.0),
        )
        .where(
            HourSubmission.student_id.in_(student_ids),
            HourSubmission.status == SubmissionStatus.pending,
        )
        .group_by(HourSubmission.student_id)
    )
    pending_count: dict[int, int] = {}
    pending_hours: dict[int, float] = {}
    for sid, n, hrs in (await db.execute(pending_q)).all():
        pending_count[sid] = int(n)
        pending_hours[sid] = float(hrs)

    # Shift IDs a student has already logged (submission of any status). A logged shift is
    # counted by its submission — not its scheduled length — so it drops out of the estimate
    # below. A rejected submission thus removes the shift from the projection entirely.
    logged_by_student: dict[int, set[int]] = {}
    for sid, shid in (
        await db.execute(
            select(HourSubmission.student_id, HourSubmission.shift_id).where(
                HourSubmission.student_id.in_(student_ids),
                HourSubmission.shift_id.is_not(None),
            )
        )
    ).all():
        logged_by_student.setdefault(sid, set()).add(shid)

    # Signed-up shifts. `upcoming_count` counts those not yet ended; the projection estimate
    # adds the scheduled length of every signed-up shift the student hasn't logged yet —
    # including ones that have already ended — so projected stays stable until the hours are
    # approved (then counted at their real value) or rejected (dropped).
    now = now_utc()
    signups = (
        await db.execute(
            select(Signup.student_id, Signup.shift_id, Shift.start_time, Shift.end_time)
            .join(Shift, Shift.id == Signup.shift_id)
            .where(
                Signup.student_id.in_(student_ids),
                Signup.status == SignupStatus.signed_up,
            )
        )
    ).all()
    scheduled_extra: dict[int, float] = {}
    upcoming_count: dict[int, int] = {}
    for sid, shift_id, start, end in signups:
        if end >= now:
            upcoming_count[sid] = upcoming_count.get(sid, 0) + 1
        if shift_id not in logged_by_student.get(sid, ()):
            scheduled_extra[sid] = scheduled_extra.get(sid, 0.0) + shift_length_hours(start, end)

    reqs = await level_requirements_map(db)

    rows = []
    for s in students:
        approved = round(approved_by_student.get(s.id, 0.0), 2)
        projected = round(
            approved + pending_hours.get(s.id, 0.0) + scheduled_extra.get(s.id, 0.0), 2
        )
        required = reqs.get(s.level, 0.0)
        rows.append({
            "student": s,
            "approved": approved,
            "projected": projected,
            "required": required,
            "remaining": round(max(0.0, required - approved), 2),
            "pct": min(100, round((approved / required) * 100)) if required else 100,
            "projected_pct": min(100, round((projected / required) * 100)) if required else 100,
            "pending_count": pending_count.get(s.id, 0),
            "upcoming_count": upcoming_count.get(s.id, 0),
            "met": approved >= required,
        })
    return rows


async def student_vhours_message(db: AsyncSession, student: Student) -> str:
    """The mrkdwn body of the `/vhours` reply — season progress, projected total, upcoming
    shifts, and a one-tap dashboard link. Shared by the Slack slash command and the admin
    'Notify students' action so both stay identical."""
    total = await season_total_hours(db, student.id)
    required = await resolve_required_hours(db, student.level)
    on_track = total >= required
    icon = "✅" if on_track else "⚠️"

    # Upcoming (or in-progress) shifts this student is signed up for, plus the projected
    # total they'd reach after completing them.
    upcoming = await upcoming_signups_for_student(db, student.id)
    projected = total + sum(
        shift_length_hours(su.shift.start_time, su.shift.end_time) for su in upcoming
    )

    reply = (
        f"{icon} *Your Volunteer Hours*\n"
        f"Season total: *{total:.1f} / {required:.1f} hrs*"
    )
    if projected > total:
        reply += f"\nProjected with upcoming shifts: *{projected:.1f} hrs*"
    if on_track:
        reply += "\nYou've met your requirement — great work! 💪"
    else:
        reply += f"\n_{required - total:.1f} hrs still needed this season._"

    if upcoming:
        reply += "\n\n*Upcoming shifts:*"
        for su in upcoming:
            opp = su.shift.opportunity.name if su.shift.opportunity else "Volunteer shift"
            reply += f"\n• {opp} — {format_shift_range(su.shift.start_time, su.shift.end_time)}"

    # A plain mrkdwn hyperlink (not an interactive button) so it just opens the URL.
    reply += f"\n\n<{magic_link(student.id)}|📊 Open my dashboard>"
    return reply
