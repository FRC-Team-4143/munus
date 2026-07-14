"""
Hour-submission lifecycle: create a pending submission, DM the chosen reviewer with
Approve/Reject buttons, apply the reviewer's decision, and notify the student.

Slack block building and DMs live here (importing only slack_client) so the Slack
router and the student portal can both trigger notifications without a circular import.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    HourSubmission, Mentor, Opportunity, Shift, Signup, SignupStatus, Student,
    SubmissionStatus,
)
from app.services.slack_client import send_dm
from app.utils import format_shift_range, shift_length_hours, utc_to_local


async def create_submission(
    db: AsyncSession,
    *,
    student_id: int,
    opportunity_id: Optional[int],
    shift_id: Optional[int],
    hours: float,
    report: Optional[str],
    reviewer_mentor_id: Optional[int],
    status: SubmissionStatus = SubmissionStatus.pending,
    submitted_at: Optional[datetime] = None,
) -> HourSubmission:
    """Create a submission and commit it.

    Defaults to a *pending* submission dated now (the normal student/Slack path). Admins
    backfilling historical hours can pass `status=approved` and a past `submitted_at`; a
    decided status also stamps `reviewed_at`.
    """
    now = datetime.utcnow()
    submission = HourSubmission(
        student_id=student_id,
        opportunity_id=opportunity_id,
        shift_id=shift_id,
        hours=hours,
        report=report,
        reviewer_mentor_id=reviewer_mentor_id,
        status=status,
        submitted_at=submitted_at or now,
        reviewed_at=now if status != SubmissionStatus.pending else None,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return submission


def resolve_reviewer_id(shift: Shift) -> Optional[int]:
    """The mentor who approves hours for a shift: shift override, else opportunity default."""
    if shift.reviewer_mentor_id is not None:
        return shift.reviewer_mentor_id
    if shift.opportunity is not None:
        return shift.opportunity.reviewer_mentor_id
    return None


async def submit_opportunity_hours(
    db: AsyncSession, student_id: int, opportunity: Opportunity, hours: float, report: Optional[str]
) -> HourSubmission:
    """Create a pending submission logged directly against a continuous (shift-less)
    opportunity, routing to its default reviewer. Unlike `submit_shift_hours`, there's
    no duplicate/idempotency guard — a student logging hours against an ongoing
    activity is expected to do so repeatedly over the season, not once per occurrence."""
    return await create_submission(
        db,
        student_id=student_id,
        opportunity_id=opportunity.id,
        shift_id=None,
        hours=hours,
        report=report,
        reviewer_mentor_id=opportunity.reviewer_mentor_id,
    )


async def submit_shift_hours(
    db: AsyncSession, signup: Signup, hours: float, report: Optional[str]
) -> Optional[HourSubmission]:
    """Create a pending submission for a signed-up shift, routing to the resolved reviewer.

    Returns None if the student already has a submission for this shift (idempotent — the
    student may tap the DM button more than once).
    """
    existing = (
        await db.execute(
            select(HourSubmission.id).where(
                HourSubmission.student_id == signup.student_id,
                HourSubmission.shift_id == signup.shift_id,
            )
        )
    ).scalars().first()
    if existing:
        return None

    return await create_submission(
        db,
        student_id=signup.student_id,
        opportunity_id=signup.shift.opportunity_id,
        shift_id=signup.shift_id,
        hours=hours,
        report=report,
        reviewer_mentor_id=resolve_reviewer_id(signup.shift),
    )


async def set_status(
    db: AsyncSession,
    submission_id: int,
    status: SubmissionStatus,
    review_note: Optional[str] = None,
) -> Optional[HourSubmission]:
    """Apply a review decision. Returns the updated submission, or None if not found."""
    submission = (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.student),
                selectinload(HourSubmission.opportunity),
            )
            .where(HourSubmission.id == submission_id)
        )
    ).scalars().first()
    if submission is None:
        return None
    submission.status = status
    submission.reviewed_at = datetime.utcnow()
    if review_note is not None:
        submission.review_note = review_note
    await db.commit()
    return submission


def _submission_summary(submission: HourSubmission) -> str:
    """One-line human description of what was submitted (for Slack/audit text)."""
    opp = submission.opportunity.name if submission.opportunity else "Volunteer work"
    when = ""
    if submission.shift is not None:
        when = f" · {format_shift_range(submission.shift.start_time, submission.shift.end_time)}"
    return f"{opp}{when} · {submission.hours:.1f} hrs"


def reviewer_blocks(submission: HourSubmission) -> list[dict]:
    """Approve/Reject DM blocks sent to the reviewing mentor."""
    student = submission.student
    opp = submission.opportunity.name if submission.opportunity else "Volunteer work"
    when = (
        format_shift_range(submission.shift.start_time, submission.shift.end_time)
        if submission.shift is not None
        else utc_to_local(submission.submitted_at).strftime("%b %d")
    )
    report = submission.report or "_No report provided._"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Hour Submission — {student.name}*\n"
                    f"*{opp}* · {when}\n"
                    f"Hours claimed: *{submission.hours:.1f}*\n"
                    f"Report: {report}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"review_{submission.id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "submission_approve",
                    "value": str(submission.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Edit hours"},
                    "action_id": "review_edit",
                    "value": str(submission.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Reject"},
                    "style": "danger",
                    "action_id": "submission_reject",
                    "value": str(submission.id),
                },
            ],
        },
    ]


def review_hours_modal(submission: HourSubmission) -> dict:
    """Slack modal for a reviewing mentor to correct a submission's hours (and report)
    before deciding — the approver-side counterpart to the student's `log_hours_modal`."""
    opp = submission.opportunity.name if submission.opportunity else "Volunteer work"
    when = (
        format_shift_range(submission.shift.start_time, submission.shift.end_time)
        if submission.shift is not None
        else utc_to_local(submission.submitted_at).strftime("%b %d")
    )
    report_element = {"type": "plain_text_input", "action_id": "value", "multiline": True}
    if submission.report:
        report_element["initial_value"] = submission.report
    return {
        "type": "modal",
        "callback_id": "review_hours",
        "private_metadata": str(submission.id),
        "title": {"type": "plain_text", "text": "Edit Hours"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{submission.student.name}* — *{opp}*\n{when}",
                },
            },
            {
                "type": "input",
                "block_id": "hours",
                "label": {"type": "plain_text", "text": "Hours"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "initial_value": f"{submission.hours:.1f}",
                },
            },
            {
                "type": "input",
                "block_id": "report",
                "optional": True,
                "label": {"type": "plain_text", "text": "Report (optional)"},
                "element": report_element,
            },
        ],
    }


def post_shift_blocks(signup: Signup, default_hours: float) -> list[dict]:
    """DM blocks prompting a student to log hours after a shift, with a one-tap default."""
    shift = signup.shift
    opp = shift.opportunity.name if shift.opportunity else "your volunteer shift"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"📝 *Log your hours — {opp}*\n"
                    f"{format_shift_range(shift.start_time, shift.end_time)}\n"
                    f"Scheduled: *{default_hours:.1f} hrs*"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"posthours_{signup.id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"✅ Log {default_hours:.1f} hrs"},
                    "style": "primary",
                    "action_id": "hours_quick",
                    "value": str(signup.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Change hours"},
                    "action_id": "hours_adjust",
                    "value": str(signup.id),
                },
            ],
        },
    ]


def log_hours_modal(signup: Signup, default_hours: float) -> dict:
    """Slack modal to adjust the logged duration (and add a note) for a shift."""
    shift = signup.shift
    opp = shift.opportunity.name if shift.opportunity else "Volunteer shift"
    return {
        "type": "modal",
        "callback_id": "log_hours",
        "private_metadata": str(signup.id),
        "title": {"type": "plain_text", "text": "Log Hours"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{opp}*\n{format_shift_range(shift.start_time, shift.end_time)}",
                },
            },
            {
                "type": "input",
                "block_id": "hours",
                "label": {"type": "plain_text", "text": "Hours volunteered"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "initial_value": f"{default_hours:.1f}",
                },
            },
            {
                "type": "input",
                "block_id": "report",
                "optional": True,
                "label": {"type": "plain_text", "text": "What did you do? (optional)"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
        ],
    }


async def notify_reviewer(submission_id: int) -> None:
    """Background task: DM the chosen reviewer with the submission + Approve/Reject buttons."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        submission = (
            await db.execute(
                select(HourSubmission)
                .options(
                    selectinload(HourSubmission.student),
                    selectinload(HourSubmission.opportunity),
                    selectinload(HourSubmission.shift),
                    selectinload(HourSubmission.reviewer),
                )
                .where(HourSubmission.id == submission_id)
            )
        ).scalars().first()
        if submission is None or submission.reviewer is None:
            return
        reviewer = submission.reviewer
        if not reviewer.slack_user_id:
            return
        await send_dm(
            reviewer.slack_user_id,
            f"New hour submission from {submission.student.name} to review",
            blocks=reviewer_blocks(submission),
        )


async def notify_student_of_review(submission_id: int) -> None:
    """Background task: DM the student the outcome of their submission review."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        submission = (
            await db.execute(
                select(HourSubmission)
                .options(
                    selectinload(HourSubmission.student),
                    selectinload(HourSubmission.opportunity),
                    selectinload(HourSubmission.shift),
                    selectinload(HourSubmission.reviewer),
                )
                .where(HourSubmission.id == submission_id)
            )
        ).scalars().first()
        if submission is None or not submission.student.slack_user_id:
            return

        reviewer_name = submission.reviewer.name if submission.reviewer else "A mentor"
        summary = _submission_summary(submission)
        if submission.status == SubmissionStatus.approved:
            text = (
                f"✅ *Hours Approved*\n{summary}\n"
                f"Approved by {reviewer_name}. These hours now count toward your season total."
            )
        elif submission.status == SubmissionStatus.rejected:
            note = f"\n_Reason: {submission.review_note}_" if submission.review_note else ""
            text = (
                f"🚫 *Hours Rejected*\n{summary}\n"
                f"Rejected by {reviewer_name}.{note}\n"
                f"Check in with them if you think this was a mistake."
            )
        else:
            return
        await send_dm(submission.student.slack_user_id, text)
