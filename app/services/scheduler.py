"""
APScheduler jobs:
  1. Pre-shift reminders — DM signed-up students before their shift starts.
  2. Post-shift prompts — DM signed-up students after a shift ends to submit a report.
  3. Auto-archive opportunities — retire a shift-based opportunity once its last shift
     is old enough that nothing is left to sign up for.
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    HourSubmission, Opportunity, Shift, Signup, SignupStatus, Student, SubmissionStatus,
)
from app.services import audit, submissions
from app.services.opportunities import update_announcement
from app.services.slack_client import send_dm
from app.utils import format_shift_range, shift_length_hours

log = logging.getLogger(__name__)


async def job_shift_reminders() -> None:
    """DM signed-up students whose shift starts within REMINDER_LEAD_HOURS."""
    if not settings.updates_enabled:
        log.info("Shift reminders skipped (updates_enabled=false)")
        return
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
                .join(Student, Student.id == Signup.student_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Signup.reminded_at.is_(None),
                    Shift.start_time > now,
                    Shift.start_time <= horizon,
                    Student.is_active.is_(True),
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
                await send_dm(student.slack_user_id, text, automated=True)
            signup.reminded_at = now
        await db.commit()
    log.info("Shift reminders: processed %d signup(s)", len(signups))


async def job_post_shift_prompts() -> None:
    """DM signed-up students after their shift ends, prompting them to submit hours."""
    if not settings.updates_enabled:
        log.info("Post-shift prompts skipped (updates_enabled=false)")
        return
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
                .join(Student, Student.id == Signup.student_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Signup.prompted_at.is_(None),
                    Shift.end_time <= now,
                    Student.is_active.is_(True),
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
                    automated=True,
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
    if not settings.updates_enabled:
        log.info("Auto-reject unlogged shifts skipped (updates_enabled=false)")
        return
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
                .join(Student, Student.id == Signup.student_id)
                .where(
                    Signup.status == SignupStatus.signed_up,
                    Shift.end_time <= cutoff,
                    Student.is_active.is_(True),
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


async def job_pending_review_reminders() -> None:
    """Re-DM the reviewing mentor about submissions still pending PENDING_REMINDER_DAYS
    after they were logged, then again every PENDING_REMINDER_DAYS until a decision is
    made. Nothing is ever removed — a pending submission sits in limbo indefinitely; this
    just keeps nudging the approver. The nudge is the standard review card (Approve / Edit
    hours / Reject), so it's actionable in place. Disabled when PENDING_REMINDER_DAYS <= 0.
    Debounced by HourSubmission.reminder_sent_at."""
    if not settings.updates_enabled:
        log.info("Pending-review reminders skipped (updates_enabled=false)")
        return
    days = settings.pending_reminder_days
    if days <= 0:
        return
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        subs = (
            await db.execute(
                select(HourSubmission)
                .options(
                    selectinload(HourSubmission.student),
                    selectinload(HourSubmission.opportunity),
                    selectinload(HourSubmission.shift),
                    selectinload(HourSubmission.reviewer),
                )
                .join(Student, Student.id == HourSubmission.student_id)
                .where(
                    HourSubmission.status == SubmissionStatus.pending,
                    HourSubmission.submitted_at <= cutoff,
                    or_(
                        HourSubmission.reminder_sent_at.is_(None),
                        HourSubmission.reminder_sent_at <= cutoff,
                    ),
                    Student.is_active.is_(True),
                )
            )
        ).scalars().all()

        nudged = 0
        for submission in subs:
            reviewer = submission.reviewer
            # No resolvable reviewer to nudge (unrouted → Admin queue, or archived) —
            # skip without stamping so a later reviewer assignment still triggers one.
            if reviewer is None or not reviewer.is_active or not reviewer.slack_user_id:
                continue
            await send_dm(
                reviewer.slack_user_id,
                f"Reminder: {submission.student.name}'s hour submission is still waiting "
                f"for your review.",
                blocks=submissions.reviewer_blocks(submission),
                automated=True,
            )
            submission.reminder_sent_at = now
            nudged += 1
        await db.commit()
    log.info("Pending-review reminders: nudged %d submission(s)", nudged)


async def job_auto_archive_opportunities() -> None:
    """Archive a shift-based opportunity once its last shift ended more than
    AUTO_ARCHIVE_DAYS ago. Archiving only flips is_active/archived_at — it never touches
    HourSubmission rows, so hours already logged against it keep counting toward
    students' season totals (only the separate /purge action deletes those). Continuous
    opportunities have no shifts and are never auto-archived — close those manually.
    Disabled when AUTO_ARCHIVE_DAYS <= 0. Idempotent: only currently-active opportunities
    are considered, so an already-archived one is never touched twice."""
    if not settings.updates_enabled:
        log.info("Auto-archive opportunities skipped (updates_enabled=false)")
        return
    days = settings.auto_archive_days
    if days <= 0:
        return
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        opps = (
            await db.execute(
                select(Opportunity)
                .options(selectinload(Opportunity.shifts))
                .where(Opportunity.is_active.is_(True), Opportunity.is_continuous.is_(False))
            )
        ).scalars().all()

        newly_archived = []
        for opp in opps:
            if not opp.shifts:
                continue  # never had a shift yet — nothing to measure "last shift" from
            last_end = max(s.end_time for s in opp.shifts)
            if last_end <= cutoff:
                opp.is_active = False
                opp.archived_at = now
                await audit.record(
                    db, None, "opportunity.auto_archive",
                    f"Auto-archived opportunity {opp.name} ({days}d after its last shift ended)",
                    entity_type="opportunity", entity_id=opp.id,
                )
                newly_archived.append(opp)
        await db.commit()
        # Keep an already-posted announcement from advertising a now-closed opportunity
        # indefinitely — no-op via update_announcement for one that was never announced.
        for opp in newly_archived:
            await update_announcement(db, opp)
    log.info("Auto-archive: archived %d opportunity(ies)", len(newly_archived))


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Backup failed")


async def job_legion_sync() -> None:
    """Pull the roster from Legion. No-op (with a log line) when Legion isn't configured,
    so the job is harmless before the SSO/API env vars are set."""
    if not settings.updates_enabled:
        log.info("Legion sync skipped (updates_enabled=false)")
        return
    if not settings.legion_base_url or not settings.legion_api_key:
        log.info("Legion sync skipped (LEGION_BASE_URL/LEGION_API_KEY not set)")
        return
    from app.services.legion_sync import sync_roster
    try:
        async with AsyncSessionLocal() as db:
            summary = await sync_roster(db)
        log.info("Scheduled Legion sync: %s", summary)
    except Exception:  # never let a sync failure crash the scheduler
        log.exception("Scheduled Legion sync failed")


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    """(Re)register all scheduled jobs from the current settings.

    Uses ``replace_existing=True`` so it is safe to call on a running scheduler
    to apply live changes to the backup schedule / timezone. The interval jobs
    read ``reminder_lead_hours`` / ``auto_reject_days`` / ``pending_reminder_days``
    from settings at run time, so those take effect without rescheduling.
    """
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
    scheduler.add_job(
        job_pending_review_reminders,
        IntervalTrigger(hours=6),
        id="pending_review_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        job_auto_archive_opportunities,
        IntervalTrigger(hours=6),
        id="auto_archive_opportunities",
        replace_existing=True,
    )

    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(day_of_week=settings.backup_day, hour=int(bh), minute=int(bm), timezone=settings.timezone),
        id="nightly_backup",
        replace_existing=True,
    )

    # Legion roster sync — hourly (cheap incremental pull via updated_since).
    scheduler.add_job(
        job_legion_sync,
        CronTrigger(minute=0, timezone=settings.timezone),
        id="legion_sync",
        replace_existing=True,
    )


def reschedule_all(scheduler) -> None:
    """Re-apply every job trigger from current settings on a live scheduler.

    Called after settings changes so the new backup schedule / timezone take
    effect without a restart. No-op if ``scheduler`` is None.
    """
    if scheduler is None:
        return
    register_jobs(scheduler)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    return scheduler
