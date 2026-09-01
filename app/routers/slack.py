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
    HourSubmission, Mentor, Opportunity, Shift, Signup, SignupStatus, Student,
    SubmissionStatus,
)
from app.services import audit, opportunities as opp_service, submissions
from app.services.legion_auth import make_link_url
from app.services.reports import mentor_vhours_message, student_vhours_message
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


async def _handle_munus_command(db: AsyncSession, user_id: str) -> JSONResponse | Response:
    """/munus — a bare one-tap link to Munus, no stats (mirrors Tempus's /tempus and
    Legion's /legion). Munus's /me is student-only (mentors have no personal dashboard —
    see legion/app/services/home.py's tiles_for), so a mentor lands on /opportunities
    instead, matching the home-page launcher's own tile for mentors."""
    student = (
        await db.execute(
            select(Student).where(Student.slack_user_id == user_id, Student.is_active.is_(True))
        )
    ).scalars().first()
    member_code = student.member_code if student else None
    next_path = "/me"
    if member_code is None:
        mentor = (
            await db.execute(
                select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
            )
        ).scalars().first()
        if mentor is not None:
            member_code = mentor.member_code
            next_path = "/opportunities"
    if member_code is None:
        return Response(
            content="❌ Your Slack account isn't linked to a Munus record. Please ask an admin.",
            media_type="text/plain",
        )
    link = f"<{make_link_url(member_code, next_path)}|❤️ Open Munus>"
    return JSONResponse({
        "response_type": "ephemeral",
        "text": link,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": link}}],
    })


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

    if command not in ("/vhours", "/munus"):
        return Response(content="Unknown command.", media_type="text/plain")

    if command == "/munus":
        return await _handle_munus_command(db, user_id)

    student = (
        await db.execute(
            select(Student).where(Student.slack_user_id == user_id, Student.is_active.is_(True))
        )
    ).scalars().first()
    if not student:
        mentor = (
            await db.execute(
                select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
            )
        ).scalars().first()
        if mentor:
            # Not an error for a mentor — just nothing to report. Point them at the
            # opportunities list (they're a read-only viewer there) instead of a bare
            # "you can't use this" message.
            reply = mentor_vhours_message(mentor)
            return JSONResponse({
                "response_type": "ephemeral",
                "text": reply,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": reply}}],
            })
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
        if cb == opp_service.SIGNUP_CALLBACK:  # announcement's "View & sign up" modal
            return await _handle_opportunity_signup_submit(db, view, acting_slack_id)
        if cb == opp_service.LOG_HOURS_CALLBACK:  # announcement's "View & record hours" modal
            return await _handle_opportunity_log_hours_submit(
                db, background_tasks, view, acting_slack_id
            )
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

    # ── Anyone opening an opportunity from the channel announcement ──
    if action_id == "opportunity_view":
        # Inline, not a background task — `trigger_id` expires in ~3s (same reason
        # `hours_adjust` opens its modal here rather than deferring).
        return await _handle_opportunity_view(
            db, value, acting_slack_id, payload.get("trigger_id", "")
        )

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
        await db.execute(
            select(Mentor).where(Mentor.id == submission.reviewer_mentor_id, Mentor.is_active.is_(True))
        )
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


async def _handle_opportunity_view(
    db: AsyncSession, value: str, acting_slack_id: str, trigger_id: str
):
    """The channel announcement's "🙋 View & sign up" button — opens a modal.

    A shared-channel message can't carry a per-person link (a plain `url` button is
    rendered client-side and never reaches us, so it can't know who clicked), but the
    *click* identifies the clicker. Opening a modal uses that identity without putting
    anything in the channel: no message, no browser, no sign-in. The student picks a
    shift and submits, handled by `_handle_opportunity_signup_submit`.

    Mentors get the details read-only, matching their status on the opportunity page
    (`_current_mentor` in routers/portal.py) — the announcement lands in a channel
    they're in too, so the button must not dead-end them.

    A linked student on a **continuous** opportunity skips the shift picker entirely —
    there's nothing to sign up for — and instead gets `opportunity_log_hours_modal`,
    handled by `_handle_opportunity_log_hours_submit`.

    An **archived** opportunity (either type) short-circuits before any of that. The
    announcement drops its button entirely once archived, so this only fires from a
    stale Slack client that rendered the button before the message was updated — still
    worth a real notice-only modal rather than a bare 200, since "nothing happened" on
    a tap someone actually made is indistinguishable from a broken button.
    """
    try:
        opp_id = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid opportunity id")
    if not trigger_id:
        return Response(status_code=200)

    opp = (
        await db.execute(
            select(Opportunity)
            .options(selectinload(Opportunity.shifts))
            .where(Opportunity.id == opp_id)
        )
    ).scalars().first()
    if opp is None:
        return Response(status_code=200)

    if not opp.is_active:
        # Archived closes the door for everyone the same way, regardless of type or who's
        # asking — no signup/log-hours form, just why.
        await open_modal(
            trigger_id,
            opp_service.opportunity_signup_modal(
                opp, None,
                notice="🗄️ *This opportunity has been archived* — no longer accepting signups or hours.",
            ),
        )
        return Response(status_code=200)

    student = (
        await db.execute(
            select(Student).where(
                Student.slack_user_id == acting_slack_id, Student.is_active.is_(True)
            )
        )
    ).scalars().first() if acting_slack_id else None

    notice = None
    shift_rows = None
    member_code = student.member_code if student is not None else None
    if student is None:
        mentor = (
            await db.execute(
                select(Mentor).where(
                    Mentor.slack_user_id == acting_slack_id, Mentor.is_active.is_(True)
                )
            )
        ).scalars().first() if acting_slack_id else None
        # Name the reason rather than opening an empty form — "nothing happened" is
        # indistinguishable from a broken button, and an unlinked account is fixable.
        notice = (
            "_Mentors don't sign up for shifts — this is just the details._"
            if mentor is not None
            else ("❌ Your Slack account isn't linked to a Munus student record yet. "
                  "Please ask an admin to link it.")
        )
        member_code = mentor.member_code if mentor is not None else None
    elif opp.is_continuous:
        details_url = make_link_url(member_code, f"/opportunities/{opp.id}")
        await open_modal(
            trigger_id, opp_service.opportunity_log_hours_modal(opp, details_url=details_url)
        )
        return Response(status_code=200)
    else:
        shift_rows = await opp_service.shift_options_for_modal(db, opp, student.id)
        if not shift_rows:
            notice = "_No upcoming shifts on this opportunity right now._"

    # A per-person magic link is safe in a modal — unlike the announcement it came from,
    # a modal is shown to exactly the one person who opened it. Omitted for an unlinked
    # caller, who has no member to sign in as.
    details_url = (
        make_link_url(member_code, f"/opportunities/{opp.id}") if member_code else None
    )

    await open_modal(
        trigger_id,
        opp_service.opportunity_signup_modal(
            opp, shift_rows, notice=notice, details_url=details_url
        ),
    )
    return Response(status_code=200)


async def _handle_opportunity_signup_submit(db: AsyncSession, view: dict, acting_slack_id: str):
    """Submission of the "View & sign up" modal — signs the student up for the shift
    they picked, reusing the same `signup_student` the web portal calls so capacity and
    duplicate handling can't drift between the two."""
    try:
        opp_id = int(view.get("private_metadata", ""))
    except ValueError:
        return Response(status_code=200)

    selected = (
        view.get("state", {}).get("values", {})
        .get("shift", {}).get("value", {})
        .get("selected_option") or {}
    )
    try:
        shift_id = int(selected.get("value", ""))
    except (TypeError, ValueError):
        return _modal_error("shift", "Pick a shift.")

    student = (
        await db.execute(
            select(Student).where(
                Student.slack_user_id == acting_slack_id, Student.is_active.is_(True)
            )
        )
    ).scalars().first() if acting_slack_id else None
    if student is None:
        return _modal_error("shift", "Your Slack account isn't linked to a student record.")

    # Opportunity.is_active guards a modal opened before an archive raced with this
    # submit — the button now bounces to a notice-only modal, but a picker already open
    # in someone's hand isn't retracted.
    shift = (
        await db.execute(
            select(Shift)
            .join(Opportunity)
            .where(
                Shift.id == shift_id, Shift.opportunity_id == opp_id,
                Opportunity.is_active.is_(True),
            )
        )
    ).scalars().first()
    if shift is None:
        return _modal_error("shift", "That shift is no longer available.")

    ok, message = await opp_service.signup_student(db, shift, student.id)
    if not ok:
        # Surface "full" / "already signed up" on the field itself — the modal stays
        # open so they can pick another shift instead of losing the dialog.
        return _modal_error("shift", message)
    return Response(status_code=200)


async def _handle_opportunity_log_hours_submit(
    db: AsyncSession, background_tasks: BackgroundTasks, view: dict, acting_slack_id: str
):
    """Submission of a continuous opportunity's "View & record hours" modal — creates a
    pending submission logged directly against the opportunity, reusing the same
    `submit_opportunity_hours` the web `/opportunities/{id}/log-hours` form calls so
    reviewer routing can't drift between the two. No idempotency guard here either,
    matching `submit_opportunity_hours` — logging repeatedly against an ongoing
    opportunity over the season is expected, not a duplicate to reject.

    `opp.is_active` is checked here too, not just in `_handle_opportunity_view` — the
    button bounces an archived opportunity to a notice-only modal now, but a log-hours
    form already open in someone's hand when the archive happened still needs to be
    rejected rather than silently accepted."""
    try:
        opp_id = int(view.get("private_metadata", ""))
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
        return _modal_error("hours", "Enter a positive number of hours.")

    student = (
        await db.execute(
            select(Student).where(
                Student.slack_user_id == acting_slack_id, Student.is_active.is_(True)
            )
        )
    ).scalars().first() if acting_slack_id else None
    if student is None:
        return Response(status_code=200)  # close silently — account changed mid-flow

    opp = (
        await db.execute(select(Opportunity).where(Opportunity.id == opp_id))
    ).scalars().first()
    if opp is None or not opp.is_continuous:
        return Response(status_code=200)
    if not opp.is_active:
        # The button now bounces to a notice-only modal, but a form already open in
        # someone's hand when the archive happened needs to say so rather than just
        # closing as if the hours were logged.
        return _modal_error("hours", "This opportunity has been archived.")

    submission = await submissions.submit_opportunity_hours(
        db, student.id, opp, round(hours, 2), report_raw.strip() if report_raw else None
    )
    reviewer_name = await _reviewer_name(db, submission)
    dest = f"sent to {reviewer_name} for approval" if reviewer_name else "sent for review"
    background_tasks.add_task(submissions.notify_reviewer, submission.id)
    background_tasks.add_task(
        send_dm, student.slack_user_id, f"✅ Logged {submission.hours:g} hrs — {dest}."
    )
    return Response(status_code=200)  # empty 200 closes the modal


def _modal_error(block_id: str, message: str) -> JSONResponse:
    """Keep a modal open with an inline error under `block_id` (Slack's `errors` action)."""
    return JSONResponse({"response_action": "errors", "errors": {block_id: message}})


async def _is_mentor(db: AsyncSession, acting_slack_id: str) -> bool:
    """True if the acting Slack user is a known, active mentor (guards the
    reviewer-only edit modal and the Approve/Reject actions) — an archived mentor
    must not be able to review submissions via Slack."""
    if not acting_slack_id:
        return False
    m = (
        await db.execute(
            select(Mentor).where(Mentor.slack_user_id == acting_slack_id, Mentor.is_active.is_(True))
        )
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
        done_msg=lambda s, dest: f"✅ Logged {s.hours:.2f} hrs — {dest}.",
    )
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
            text=(f"⚠️ Couldn't open the hours form. Tap *✅ Log {default_hours:.2f} hrs* "
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
        done_msg=lambda s, dest: f"✅ Logged {s.hours:.2f} hrs — {dest}.",
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
        f"{actor} {verb} {submission.student.name}'s submission ({submission.hours:.2f} hrs) via Slack",
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
                    f"{submission.hours:.2f} hrs · the student has been notified."
                ),
            },
        }],
        replace_original=True,
    )
    return Response(status_code=200)
