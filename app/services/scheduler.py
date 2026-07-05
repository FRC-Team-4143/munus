"""
APScheduler jobs:
  1. Pre-shift reminders — DM signed-up students before their shift starts.
  2. Post-shift prompts — DM signed-up students after a shift ends to submit a report.
  3. Weekly season-progress DM — approved hours vs each student's level requirement.
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    HourSubmission, Shift, Signup, SignupStatus, Student, SubmissionStatus,
)
from app.services import submissions
from app.services.requirements import resolve_required_hours, season_total_hours
from app.services.slack_client import send_dm
from app.services.student_auth import magic_link
from app.utils import format_shift_range, shift_length_hours

log = logging.getLogger(__name__)


async def job_shift_reminders() -> None:
    """DM signed-up students whose shift starts within REMINDER_LEAD_HOURS."""
    now = datetime.utcnow()
    horizon = now + timedelta(hours=settings.reminder_lead_hours)
    async with AsyncSessionLocal() as db:
        signups = (
            await db.execute(
                select(Signup)
                .options(
                    selectinload(Signup.student),
                    selectinload(Signup.shift).selectinload(Shift.opportunity),
                )
                .join(Shift, Shift.id == Signup.shift_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Signup.reminded_at.is_(None),
                    Shift.start_time > now,
                    Shift.start_time <= horizon,
                )
            )
        ).scalars().all()

        for signup in signups:
            student = signup.student
            shift = signup.shift
            if student.slack_user_id:
                o = shift.opportunity
                opp = o.name if o else "Volunteer shift"
                text = (
                    f"⏰ *Upcoming Shift Reminder*\n"
                    f"*{opp}*\n{format_shift_range(shift.start_time, shift.end_time)}"
                )
                if o and o.location:
                    text += f"\nLocation: {o.location}"
                if o and o.attire:
                    text += f"\nAttire: {o.attire}"
                await send_dm(student.slack_user_id, text)
            signup.reminded_at = now
        await db.commit()
    log.info("Shift reminders: processed %d signup(s)", len(signups))


async def job_post_shift_prompts() -> None:
    """DM signed-up students after their shift ends, prompting them to submit hours."""
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        signups = (
            await db.execute(
                select(Signup)
                .options(
                    selectinload(Signup.student),
                    selectinload(Signup.shift).selectinload(Shift.opportunity),
                )
                .join(Shift, Shift.id == Signup.shift_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Signup.prompted_at.is_(None),
                    Shift.end_time <= now,
                )
            )
        ).scalars().all()

        prompted = 0
        for signup in signups:
            student = signup.student
            shift = signup.shift
            # Skip if they already submitted hours for this shift.
            already = (
                await db.execute(
                    select(HourSubmission.id).where(
                        HourSubmission.student_id == student.id,
                        HourSubmission.shift_id == shift.id,
                    )
                )
            ).scalars().first()
            if not already and student.slack_user_id:
                default_hours = shift_length_hours(shift.start_time, shift.end_time)
                await send_dm(
                    student.slack_user_id,
                    "Log your volunteer hours",
                    blocks=submissions.post_shift_blocks(signup, default_hours),
                )
                prompted += 1
            signup.prompted_at = now
        await db.commit()
    log.info("Post-shift prompts: sent %d prompt(s)", prompted)


async def job_auto_reject_unlogged() -> None:
    """Auto-reject signed-up shifts a student never logged within AUTO_REJECT_DAYS of the
    shift ending. Records a rejected HourSubmission so the miss is on file and the shift
    stops counting toward the student's projected hours. Disabled when AUTO_REJECT_DAYS <= 0.
    Idempotent: a shift with any existing submission is skipped, so it never double-rejects."""
    days = settings.auto_reject_days
    if days <= 0:
        return
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        signups = (
            await db.execute(
                select(Signup)
                .options(
                    selectinload(Signup.student),
                    selectinload(Signup.shift).selectinload(Shift.opportunity),
                )
                .join(Shift, Shift.id == Signup.shift_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Shift.end_time <= cutoff,
                )
            )
        ).scalars().all()

        rejected = 0
        for signup in signups:
            student = signup.student
            shift = signup.shift
            # Skip if the student already has a submission for this shift (of any status).
            already = (
                await db.execute(
                    select(HourSubmission.id).where(
                        HourSubmission.student_id == signup.student_id,
                        HourSubmission.shift_id == shift.id,
                    )
                )
            ).scalars().first()
            if already:
                continue

            db.add(HourSubmission(
                student_id=signup.student_id,
                opportunity_id=shift.opportunity_id,
                shift_id=shift.id,
                hours=shift_length_hours(shift.start_time, shift.end_time),
                report=None,
                reviewer_mentor_id=submissions.resolve_reviewer_id(shift),
                status=SubmissionStatus.rejected,
                submitted_at=now,
                reviewed_at=now,
                review_note=f"Auto-rejected — hours not submitted within {days} days of the shift.",
            ))
            rejected += 1

            if student.slack_user_id:
                o = shift.opportunity
                opp = o.name if o else "your volunteer shift"
                await send_dm(
                    student.slack_user_id,
                    f"⌛ *Hours window closed — {opp}*\n"
                    f"{format_shift_range(shift.start_time, shift.end_time)}\n"
                    f"We didn't get your hours within {days} days, so this shift was closed "
                    f"out and won't count toward your season total. If you did volunteer, ask "
                    f"a mentor to add the hours for you.",
                )
        await db.commit()
    log.info("Auto-reject: closed %d unlogged shift(s)", rejected)


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Backup failed")


async def job_weekly_dms() -> None:
    """DM each active, Slack-linked student their season progress vs their requirement."""
    async with AsyncSessionLocal() as db:
        students = (
            await db.execute(
                select(Student).where(
                    Student.is_active.is_(True),
                    Student.slack_user_id.is_not(None),
                )
            )
        ).scalars().all()

        for student in students:
            total = await season_total_hours(db, student.id)
            required = await resolve_required_hours(db, student.level)
            on_track = total >= required
            icon = "✅" if on_track else "⚠️"
            text = (
                f"{icon} *Volunteer Hours — {student.name}*\n"
                f"Season total: *{total:.1f} / {required:.1f} hrs*"
            )
            if on_track:
                text += "\nYou've met your requirement — nice work! 💪"
            else:
                text += f"\n_{required - total:.1f} hrs still needed this season._"
            text += f"\n\n📊 Your dashboard: {magic_link(student.id)}"
            await send_dm(student.slack_user_id, text)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    scheduler.add_job(
        job_shift_reminders,
        IntervalTrigger(minutes=30),
        id="shift_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        job_post_shift_prompts,
        IntervalTrigger(minutes=30),
        id="post_shift_prompts",
        replace_existing=True,
    )
    scheduler.add_job(
        job_auto_reject_unlogged,
        IntervalTrigger(hours=6),
        id="auto_reject_unlogged",
        replace_existing=True,
    )

    dh, dm_ = settings.weekly_dm_time.split(":")
    scheduler.add_job(
        job_weekly_dms,
        CronTrigger(
            day_of_week=settings.weekly_dm_day,
            hour=int(dh),
            minute=int(dm_),
            timezone=settings.timezone,
        ),
        id="weekly_dms",
        replace_existing=True,
    )

    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(day_of_week=settings.backup_day, hour=int(bh), minute=int(bm), timezone=settings.timezone),
        id="nightly_backup",
        replace_existing=True,
    )

    return scheduler
