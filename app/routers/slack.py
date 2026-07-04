"""
Slack routes — slash command and interactive component handler.

Slack sends:
  POST /slack/command   — slash commands (verified by signing secret)
  POST /slack/interact  — interactive button actions (verified by signing secret)
"""
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Mentor, Student, SubmissionStatus
from app.services import audit, submissions
from app.services.requirements import resolve_required_hours, season_total_hours

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    # Reject requests older than 5 minutes (replay protection)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")
    return body


# ── Slash command router ───────────────────────────────────────────────────────

@router.post("/command")
async def slack_command(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _verify_slack_signature(request)

    form = await request.form()
    command = form.get("command", "")
    user_id = form.get("user_id", "")

    if command != "/vhours":
        return Response(content="Unknown command.", media_type="text/plain")

    student = (
        await db.execute(select(Student).where(Student.slack_user_id == user_id))
    ).scalars().first()
    if not student:
        return Response(
            content="❌ Your Slack account isn't linked to a student record. Please ask an admin.",
            media_type="text/plain",
        )

    total = await season_total_hours(db, student.id)
    required = await resolve_required_hours(db, student.level)
    on_track = total >= required
    icon = "✅" if on_track else "⚠️"

    reply = (
        f"{icon} *Your Volunteer Hours*\n"
        f"Season total: *{total:.1f} / {required:.1f} hrs*"
    )
    if on_track:
        reply += "\nYou've met your requirement — great work! 💪"
    else:
        reply += f"\n_{required - total:.1f} hrs still needed this season._"
    return Response(content=reply, media_type="text/plain")


# ── Interactive actions handler (Approve / Reject) ─────────────────────────────

@router.post("/interact")
async def slack_interact(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    await _verify_slack_signature(request)

    form = await request.form()
    payload_str = form.get("payload", "")
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload")

    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    submission_id_str = action.get("value", "")
    reviewer_slack_id = payload.get("user", {}).get("id", "")
    response_url = payload.get("response_url", "")

    if action_id not in ("submission_approve", "submission_reject"):
        return Response(status_code=200)

    try:
        submission_id = int(submission_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid submission id")

    from slack_sdk.webhook.async_client import AsyncWebhookClient

    status = (
        SubmissionStatus.approved if action_id == "submission_approve"
        else SubmissionStatus.rejected
    )
    submission = await submissions.set_status(db, submission_id, status)
    if submission is None:
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text="⚠️ Submission not found.",
            replace_original=True,
        )
        return Response(status_code=200)

    # Audit — record who decided, resolving the Slack ID to a mentor name if known.
    reviewer = (
        await db.execute(select(Mentor).where(Mentor.slack_user_id == reviewer_slack_id))
    ).scalars().first()
    actor = reviewer.name if reviewer else reviewer_slack_id
    verb = "approved" if status == SubmissionStatus.approved else "rejected"
    await audit.record(
        db, request, f"submission.{verb}",
        f"{actor} {verb} {submission.student.name}'s submission ({submission.hours:.1f} hrs) via Slack",
        entity_type="submission", entity_id=submission.id, actor=actor,
        detail={"student": submission.student.name, "hours": submission.hours, "via": "slack"},
    )
    await db.commit()

    background_tasks.add_task(submissions.notify_student_of_review, submission.id)

    icon = "✅" if status == SubmissionStatus.approved else "🚫"
    background_tasks.add_task(
        AsyncWebhookClient(response_url).send,
        text=f"Submission {verb}",
        blocks=[{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{icon} *{verb.capitalize()} — {submission.student.name}*\n"
                    f"{submission.hours:.1f} hrs · the student has been notified."
                ),
            },
        }],
        replace_original=True,
    )
    return Response(status_code=200)
