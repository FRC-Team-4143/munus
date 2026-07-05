import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import HourSubmission, Signup, SignupStatus, SubmissionStatus


def _signed(body: str) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _interact(client, payload: dict):
    body = urlencode({"payload": json.dumps(payload)})
    return await client.post("/slack/interact", content=body, headers=_signed(body))


@pytest.fixture
def hush_slack(monkeypatch):
    """Silence outbound Slack calls (webhooks / DMs / reviewer notify) during interact tests."""
    import app.routers.slack as slackmod
    import app.services.submissions as subs
    import slack_sdk.webhook.async_client as whmod

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(subs, "notify_reviewer", _noop)
    monkeypatch.setattr(slackmod, "send_dm", _noop)

    class _FakeWebhook:
        def __init__(self, *a, **k):
            pass

        async def send(self, *a, **k):
            return None

    monkeypatch.setattr(whmod, "AsyncWebhookClient", _FakeWebhook)


async def _make_signup(db, make_student, make_opportunity, make_shift, opp_reviewer=None):
    student = await make_student(slack="U0STU")
    opp = await make_opportunity(reviewer_mentor_id=opp_reviewer)
    shift = await make_shift(opp.id, start_in_hours=-2, length_hours=2)  # ended, 2h
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()
    signup = (
        await db.execute(select(Signup).where(Signup.student_id == student.id))
    ).scalars().first()
    return student, signup


async def test_hours_quick_creates_pending_submission(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    mentor = await make_mentor(slack="U0REV")
    student, signup = await _make_signup(
        db, make_student, make_opportunity, make_shift, opp_reviewer=mentor.id
    )

    payload = {
        "type": "block_actions",
        "user": {"id": "U0STU"},
        "trigger_id": "t",
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "hours_quick", "value": str(signup.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200

    sub = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == student.id))
    ).scalars().first()
    assert sub is not None
    assert sub.status == SubmissionStatus.pending
    assert sub.hours == 2.0                     # defaulted to the scheduled duration
    assert sub.reviewer_mentor_id == mentor.id  # routed to the opportunity's approver


async def test_hours_quick_rejects_wrong_user(
    client, db, hush_slack, make_student, make_opportunity, make_shift
):
    student, signup = await _make_signup(db, make_student, make_opportunity, make_shift)
    payload = {
        "type": "block_actions",
        "user": {"id": "U0SOMEONE_ELSE"},
        "trigger_id": "t",
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "hours_quick", "value": str(signup.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert (await db.execute(select(HourSubmission))).scalars().first() is None


async def test_hours_adjust_opens_modal(
    client, db, hush_slack, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.slack as slackmod

    captured = {}

    async def fake_open_modal(trigger_id, view):
        captured["trigger_id"] = trigger_id
        captured["view"] = view
        return True

    monkeypatch.setattr(slackmod, "open_modal", fake_open_modal)

    student, signup = await _make_signup(db, make_student, make_opportunity, make_shift)
    payload = {
        "type": "block_actions",
        "user": {"id": "U0STU"},
        "trigger_id": "trig123",
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "hours_adjust", "value": str(signup.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert captured["trigger_id"] == "trig123"
    assert captured["view"]["callback_id"] == "log_hours"
    assert captured["view"]["private_metadata"] == str(signup.id)


async def test_view_submission_bad_hours_returns_errors(
    client, db, hush_slack, make_student, make_opportunity, make_shift
):
    student, signup = await _make_signup(db, make_student, make_opportunity, make_shift)
    payload = {
        "type": "view_submission",
        "user": {"id": "U0STU"},
        "view": {
            "callback_id": "log_hours",
            "private_metadata": str(signup.id),
            "state": {"values": {
                "hours": {"value": {"value": "not-a-number"}},
                "report": {"value": {"value": None}},
            }},
        },
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert resp.json().get("response_action") == "errors"
    assert (await db.execute(select(HourSubmission))).scalars().first() is None


async def test_view_submission_logs_adjusted_hours(
    client, db, hush_slack, make_student, make_opportunity, make_shift
):
    student, signup = await _make_signup(db, make_student, make_opportunity, make_shift)
    payload = {
        "type": "view_submission",
        "user": {"id": "U0STU"},
        "view": {
            "callback_id": "log_hours",
            "private_metadata": str(signup.id),
            "state": {"values": {
                "hours": {"value": {"value": "1.5"}},   # shift ran short
                "report": {"value": {"value": "Left early"}},
            }},
        },
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    sub = (await db.execute(select(HourSubmission))).scalars().first()
    assert sub is not None and sub.hours == 1.5 and sub.report == "Left early"


# ── Mentor "Edit hours" modal (approver-side) ──────────────────────────────────

async def _make_submission(db, make_student, make_mentor, make_opportunity, make_shift):
    """A pending submission routed to a Slack-linked reviewer mentor (U0REV)."""
    mentor = await make_mentor(slack="U0REV")
    student = await make_student(slack="U0STU")
    opp = await make_opportunity(reviewer_mentor_id=mentor.id)
    shift = await make_shift(opp.id, start_in_hours=-2, length_hours=2)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(
        student_id=student.id, opportunity_id=opp.id, shift_id=shift.id,
        hours=2.0, reviewer_mentor_id=mentor.id, status=SubmissionStatus.pending,
    ))
    await db.commit()
    sub = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == student.id))
    ).scalars().first()
    return mentor, student, sub


async def test_review_edit_opens_modal(
    client, db, hush_slack, monkeypatch, make_student, make_mentor, make_opportunity, make_shift
):
    import app.routers.slack as slackmod

    captured = {}

    async def fake_open_modal(trigger_id, view):
        captured["trigger_id"] = trigger_id
        captured["view"] = view
        return True

    monkeypatch.setattr(slackmod, "open_modal", fake_open_modal)

    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "U0REV"},                 # the reviewing mentor
        "trigger_id": "trigABC",
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "review_edit", "value": str(sub.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert captured["trigger_id"] == "trigABC"
    assert captured["view"]["callback_id"] == "review_hours"
    assert captured["view"]["private_metadata"] == str(sub.id)


async def test_review_edit_blocked_for_non_mentor(
    client, db, hush_slack, monkeypatch, make_student, make_mentor, make_opportunity, make_shift
):
    import app.routers.slack as slackmod

    opened = {"n": 0}

    async def fake_open_modal(trigger_id, view):
        opened["n"] += 1
        return True

    monkeypatch.setattr(slackmod, "open_modal", fake_open_modal)

    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "U0STU"},                 # a student, not a mentor
        "trigger_id": "trigABC",
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "review_edit", "value": str(sub.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert opened["n"] == 0                       # modal never opens for a non-mentor


async def test_review_hours_modal_updates_submission(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "view_submission",
        "user": {"id": "U0REV"},
        "view": {
            "callback_id": "review_hours",
            "private_metadata": str(sub.id),
            "state": {"values": {
                "hours": {"value": {"value": "3.5"}},   # mentor corrects the hours
                "report": {"value": {"value": "Adjusted by mentor"}},
            }},
        },
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    await db.refresh(sub)
    assert sub.hours == 3.5
    assert sub.report == "Adjusted by mentor"
    assert sub.status == SubmissionStatus.pending   # still awaiting the decision


async def test_review_hours_modal_bad_hours_returns_errors(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "view_submission",
        "user": {"id": "U0REV"},
        "view": {
            "callback_id": "review_hours",
            "private_metadata": str(sub.id),
            "state": {"values": {
                "hours": {"value": {"value": "-1"}},
                "report": {"value": {"value": None}},
            }},
        },
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert resp.json().get("response_action") == "errors"
    await db.refresh(sub)
    assert sub.hours == 2.0                          # unchanged
