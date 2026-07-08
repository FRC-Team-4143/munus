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
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    HourSubmission, Mentor, Shift, Signup, SignupStatus, Student, SubmissionStatus,
)
from app.services import audit, submissions
from app.services.reports import student_vhours_message
from app.services.slack_client import open_modal, send_dm
from app.utils import shift_length_hours

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="Slack integration is not configured (no signing secret set).")

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

    # Same summary the admin "Notify students" button DMs — built in one place so they match.
    # Ephemeral response (only the caller sees it); the dashboard link is a plain mrkdwn
    # hyperlink, not an interactive button, so it never fires an interaction callback.
    reply = await student_vhours_message(db, student)
    return JSONResponse({
        "response_type": "ephemeral",
        "text": reply,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reply}}],
    })


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

    ptype = payload.get("type")
    acting_slack_id = payload.get("user", {}).get("id", "")

    # ── Modal submissions ──
    if ptype == "view_submission":
        view = payload.get("view", {})
        cb = view.get("callback_id")
        if cb == "log_hours":  # student's "Change hours" modal
            return await _handle_log_hours_submit(db, background_tasks, view, acting_slack_id)
        if cb == "review_hours":  # mentor's "Edit hours" modal
            return await _handle_review_edit_submit(db, background_tasks, view, acting_slack_id)
        return Response(status_code=200)

    if ptype != "block_actions":
        return Response(status_code=200)

    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")
    response_url = payload.get("response_url", "")

    # ── Student logging hours from the post-shift DM ──
    if action_id == "hours_quick":
        return await _handle_quick_log(db, background_tasks, value, acting_slack_id, response_url)
    if action_id == "hours_adjust":
        return await _handle_adjust(
            db, background_tasks, value, acting_slack_id,
            payload.get("trigger_id", ""), response_url,
        )

    # ── Mentor editing a submission's hours before deciding ──
    if action_id == "review_edit":
        return await _handle_review_edit_open(
            db, background_tasks, value, acting_slack_id,
            payload.get("trigger_id", ""), response_url,
        )

    # ── Mentor approving/rejecting a submission ──
    if action_id in ("submission_approve", "submission_reject"):
        return await _handle_review(
            request, db, background_tasks, action_id, value, acting_slack_id, response_url
        )

    # ── Student opening an opportunity from a channel announcement ──
    if action_id == "opp_dashboard":
        return await _handle_opp_dashboard(db, background_tasks, value, acting_slack_id, response_url)

    return Response(status_code=200)


# ── /interact helpers ──────────────────────────────────────────────────────────

async def _load_signup(db: AsyncSession, signup_id: int) -> Optional[Signup]:
    return (
        await db.execute(
            select(Signup)
            .options(
                selectinload(Signup.student),
                selectinload(Signup.shift).selectinload(Shift.opportunity),
            )
            .where(Signup.id == signup_id)
        )
    ).scalars().first()


async def _reviewer_name(db: AsyncSession, submission) -> Optional[str]:
    if submission.reviewer_mentor_id is None:
        return None
    m = (
        await db.execute(select(Mentor).where(Mentor.id == submission.reviewer_mentor_id))
    ).scalars().first()
    return m.name if m else None


async def _load_submission(db: AsyncSession, submission_id: int) -> Optional[HourSubmission]:
    return (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.student),
                selectinload(HourSubmission.opportunity),
                selectinload(HourSubmission.shift),
            )
            .where(HourSubmission.id == submission_id)
        )
    ).scalars().first()


async def _is_mentor(db: AsyncSession, acting_slack_id: str) -> bool:
    """True if the acting Slack user is a known mentor (guards the reviewer-only edit modal)."""
    if not acting_slack_id:
        return False
    m = (
        await db.execute(select(Mentor).where(Mentor.slack_user_id == acting_slack_id))
    ).scalars().first()
    return m is not None


def _owns_signup(signup: Optional[Signup], acting_slack_id: str) -> bool:
    """True only if the acting Slack user is the student the signup belongs to."""
    return bool(
        signup
        and signup.status == SignupStatus.signed_up
        and signup.student.slack_user_id
        and signup.student.slack_user_id == acting_slack_id
    )


async def _finish_log(db, background_tasks, signup, submission, notify, already_msg, done_msg):
    """Shared tail for quick-log and modal-submit: DM reviewer + confirm to the student."""
    if submission is None:
        notify(already_msg)
        return
    reviewer_name = await _reviewer_name(db, submission)
    dest = f"sent to {reviewer_name} for approval" if reviewer_name else "sent for review"
    background_tasks.add_task(submissions.notify_reviewer, submission.id)
    notify(done_msg(submission, dest))


async def _handle_quick_log(db, background_tasks, value, acting_slack_id, response_url):
    from slack_sdk.webhook.async_client import AsyncWebhookClient

    def reply(text):
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send, text=text, replace_original=True
        )

    try:
        signup = await _load_signup(db, int(value))
    except ValueError:
        return Response(status_code=200)
    if not _owns_signup(signup, acting_slack_id):
        reply("⚠️ Couldn't log those hours.")
        return Response(status_code=200)

    default_hours = shift_length_hours(signup.shift.start_time, signup.shift.end_time)
    submission = await submissions.submit_shift_hours(db, signup, default_hours, None)
    await _finish_log(
        db, background_tasks, signup, submission, reply,
        already_msg="✅ You've already logged hours for this shift.",
        done_msg=lambda s, dest: f"✅ Logged {s.hours:.1f} hrs — {dest}.",
    )
    return Response(status_code=200)


async def _handle_opp_dashboard(db, background_tasks, value, acting_slack_id, response_url):
    """A student clicked '🙋 View & sign up' on a channel announcement. Slack tells us who
    clicked, so we privately (ephemeral) reply with THAT student's own one-tap sign-in link,
    deep-linked to the opportunity — a single shared button gives each person their own link.
    The link is a plain mrkdwn hyperlink (not a button) so it never fires another interaction."""
    from slack_sdk.webhook.async_client import AsyncWebhookClient

    def reply(text):
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            response_type="ephemeral",
            replace_original=False,
        )

    try:
        opp_id = int(value)
    except ValueError:
        return Response(status_code=200)

    student = (
        await db.execute(select(Student).where(Student.slack_user_id == acting_slack_id))
    ).scalars().first()
    if not student:
        reply("❌ Your Slack account isn't linked to a student record — ask an admin to link it.")
        return Response(status_code=200)

    from urllib.parse import quote
    link = f"{settings.base_url}/enter?member={student.member_code}&next={quote(f'/opportunities/{opp_id}')}"
    reply(f"<{link}|🙋 View & sign up>")
    return Response(status_code=200)


async def _handle_adjust(db, background_tasks, value, acting_slack_id, trigger_id, response_url):
    try:
        signup = await _load_signup(db, int(value))
    except ValueError:
        return Response(status_code=200)
    if not _owns_signup(signup, acting_slack_id) or not trigger_id:
        return Response(status_code=200)
    default_hours = shift_length_hours(signup.shift.start_time, signup.shift.end_time)
    ok = await open_modal(trigger_id, submissions.log_hours_modal(signup, default_hours))
    if not ok and response_url:
        # The modal couldn't open (see the server log for Slack's reason). Give the
        # student a usable fallback rather than a bare Slack error.
        from slack_sdk.webhook.async_client import AsyncWebhookClient
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text=(f"⚠️ Couldn't open the hours form. Tap *✅ Log {default_hours:.1f} hrs* "
                  f"to log the scheduled time, or ask an admin."),
            replace_original=False,
        )
    return Response(status_code=200)


async def _handle_log_hours_submit(db, background_tasks, view, acting_slack_id):
    try:
        signup_id = int(view.get("private_metadata", ""))
    except ValueError:
        return Response(status_code=200)

    values = view.get("state", {}).get("values", {})
    hours_raw = values.get("hours", {}).get("value", {}).get("value", "")
    report_raw = values.get("report", {}).get("value", {}).get("value")
    try:
        hours = float(hours_raw)
        if hours <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({
            "response_action": "errors",
            "errors": {"hours": "Enter a positive number of hours."},
        })

    signup = await _load_signup(db, signup_id)
    if not _owns_signup(signup, acting_slack_id):
        return Response(status_code=200)  # close the modal silently

    student_slack = signup.student.slack_user_id
    submission = await submissions.submit_shift_hours(
        db, signup, round(hours, 2), report_raw.strip() if report_raw else None
    )

    def dm(text):
        background_tasks.add_task(send_dm, student_slack, text)

    await _finish_log(
        db, background_tasks, signup, submission, dm,
        already_msg="You've already logged hours for this shift.",
        done_msg=lambda s, dest: f"✅ Logged {s.hours:.1f} hrs — {dest}.",
    )
    return Response(status_code=200)  # empty 200 closes the modal


async def _handle_review_edit_open(
    db, background_tasks, value, acting_slack_id, trigger_id, response_url
):
    """Mentor tapped "Edit hours" — open a modal pre-filled with the submission's hours."""
    try:
        submission = await _load_submission(db, int(value))
    except ValueError:
        return Response(status_code=200)
    if submission is None or not trigger_id or not await _is_mentor(db, acting_slack_id):
        return Response(status_code=200)

    ok = await open_modal(trigger_id, submissions.review_hours_modal(submission))
    if not ok and response_url:
        from slack_sdk.webhook.async_client import AsyncWebhookClient
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text=("⚠️ Couldn't open the edit form (see the server log). You can still "
                  "Approve/Reject here, or edit it in the admin portal."),
            replace_original=False,
        )
    return Response(status_code=200)


async def _handle_review_edit_submit(db, background_tasks, view, acting_slack_id):
    """Mentor submitted the "Edit hours" modal — update the (still pending) submission and
    re-send the review card with the corrected hours so they can approve/reject it."""
    try:
        submission_id = int(view.get("private_metadata", ""))
    except ValueError:
        return Response(status_code=200)

    values = view.get("state", {}).get("values", {})
    hours_raw = values.get("hours", {}).get("value", {}).get("value", "")
    report_raw = values.get("report", {}).get("value", {}).get("value")
    try:
        hours = float(hours_raw)
        if hours <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({
            "response_action": "errors",
            "errors": {"hours": "Enter a positive number of hours."},
        })

    if not await _is_mentor(db, acting_slack_id):
        return Response(status_code=200)  # close the modal silently
    submission = await _load_submission(db, submission_id)
    if submission is None:
        return Response(status_code=200)

    submission.hours = round(hours, 2)
    if report_raw is not None:
        submission.report = report_raw.strip() or None
    await db.commit()

    # Re-send the review card (to the assigned reviewer) reflecting the corrected hours.
    background_tasks.add_task(submissions.notify_reviewer, submission.id)
    return Response(status_code=200)  # empty 200 closes the modal


async def _handle_review(request, db, background_tasks, action_id, value, reviewer_slack_id, response_url):
    from slack_sdk.webhook.async_client import AsyncWebhookClient

    try:
        submission_id = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid submission id")

    if not await _is_mentor(db, reviewer_slack_id):
        return Response(status_code=200)  # silently ignore — not an authorized reviewer

    status = (
        SubmissionStatus.approved if action_id == "submission_approve"
        else SubmissionStatus.rejected
    )
    submission = await submissions.set_status(db, submission_id, status)
    if submission is None:
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text="⚠️ Submission not found.", replace_original=True,
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
