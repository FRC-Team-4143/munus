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

from app.models import HourSubmission, Mentor, Opportunity, Student, SubmissionStatus
from app.services.slack_client import send_dm
from app.utils import format_shift_range, utc_to_local


async def create_submission(
    db: AsyncSession,
    *,
    student_id: int,
    opportunity_id: Optional[int],
    shift_id: Optional[int],
    hours: float,
    report: Optional[str],
    reviewer_mentor_id: Optional[int],
) -> HourSubmission:
    """Create a pending submission and commit it."""
    submission = HourSubmission(
        student_id=student_id,
        opportunity_id=opportunity_id,
        shift_id=shift_id,
        hours=hours,
        report=report,
        reviewer_mentor_id=reviewer_mentor_id,
        status=SubmissionStatus.pending,
        submitted_at=datetime.utcnow(),
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return submission


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
                    "text": {"type": "plain_text", "text": "🚫 Reject"},
                    "style": "danger",
                    "action_id": "submission_reject",
                    "value": str(submission.id),
                },
            ],
        },
    ]


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
