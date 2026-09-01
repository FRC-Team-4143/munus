import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from sqlalchemy import select

from app.config import settings
from app.models import HourSubmission, Signup, SignupStatus, SubmissionStatus
from tests.conftest import magic_link_payloads


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


async def test_submission_approve_blocked_for_non_mentor(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    """Regression test: submission_approve/reject previously had no mentor check at
    all (unlike the sibling review_edit action) — any Slack user who could trigger
    the button got it processed."""
    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "U0STU"},                 # a student, not a mentor
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "submission_approve", "value": str(sub.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.pending


async def test_submission_approve_works_for_reviewer(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    _mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "U0REV"},                 # the reviewing mentor
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "submission_approve", "value": str(sub.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.approved


async def test_submission_approve_blocked_for_archived_mentor(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    """An archived mentor must not be able to approve/reject via Slack, even though
    they're still the submission's on-record reviewer — matches the non-mentor case,
    since an archived mentor is no longer a valid reviewer anywhere in the app."""
    mentor, _student, sub = await _make_submission(
        db, make_student, make_mentor, make_opportunity, make_shift
    )
    mentor.is_active = False
    await db.commit()

    payload = {
        "type": "block_actions",
        "user": {"id": "U0REV"},
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "submission_approve", "value": str(sub.id)}],
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.pending


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




# ── Announcement "🙋 View & sign up" button → modal ────────────────────────────
#
# The button lives in a shared channel, so it can't carry a per-person link (a url
# button is rendered client-side and never reaches us). Clicking it opens a modal
# instead: that uses the clicker's identity without putting anything in the channel —
# no message, no browser, no sign-in.


def _shift_picker(view: dict) -> dict:
    """The shift `static_select`, addressed by block_id rather than position — the modal
    grows blocks (details, notice, the full-details link) around it."""
    block = next(b for b in view["blocks"] if b.get("block_id") == "shift")
    return block["element"]


def _modal_text(view: dict) -> str:
    """Every section's text in one string, for asserting on wording."""
    return "\n".join(b["text"]["text"] for b in view["blocks"] if b["type"] == "section")


def _view_payload(slack_id: str, opp_id: int, trigger_id: str = "t") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": slack_id},
        "trigger_id": trigger_id,
        "response_url": "https://hooks.slack.test/x",
        "actions": [{"action_id": "opportunity_view", "value": str(opp_id)}],
    }


def _submit_payload(slack_id: str, opp_id: int, shift_id) -> dict:
    selected = (
        {"selected_option": {"value": str(shift_id), "text": {"type": "plain_text", "text": "s"}}}
        if shift_id is not None else {}
    )
    return {
        "type": "view_submission",
        "user": {"id": slack_id},
        "view": {
            "callback_id": "opportunity_signup",
            "private_metadata": str(opp_id),
            "state": {"values": {"shift": {"value": selected}}},
        },
    }


async def test_view_button_opens_a_modal_and_posts_nothing(
    client, capture_webhook, capture_modal, make_student, make_opportunity, make_shift
):
    """The whole point of the modal: the channel stays untouched."""
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity(name="Food Drive")
    shift = await make_shift(opp.id, start_in_hours=24)

    resp = await _interact(client, _view_payload("U0STU", opp.id))

    assert resp.status_code == 200
    assert capture_webhook == []  # nothing sent to the channel, ephemeral or otherwise
    (view,) = capture_modal
    assert view["callback_id"] == "opportunity_signup"
    assert view["private_metadata"] == str(opp.id)
    options = _shift_picker(view)["options"]
    assert [o["value"] for o in options] == [str(shift.id)]
    assert view["submit"]["text"] == "Sign up"


async def test_modal_marks_full_and_already_signed_up_shifts(
    client, db, capture_modal, make_student, make_opportunity, make_shift
):
    student = await make_student(slack="U0STU", code="stu00001")
    other = await make_student(name="Other", slack="U0OTH", code="oth00001")
    opp = await make_opportunity()
    joined = await make_shift(opp.id, start_in_hours=24)
    full = await make_shift(opp.id, capacity=1, start_in_hours=48)
    db.add(Signup(shift_id=joined.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=full.id, student_id=other.id, status=SignupStatus.signed_up))
    await db.commit()

    await _interact(client, _view_payload("U0STU", opp.id))

    (view,) = capture_modal
    labels = {o["value"]: o["text"]["text"] for o in _shift_picker(view)["options"]}
    assert "signed up" in labels[str(joined.id)]
    assert "FULL" in labels[str(full.id)]


async def test_modal_for_a_mentor_is_read_only(
    client, capture_modal, make_mentor, make_opportunity, make_shift
):
    """Mentors don't sign up — they get the details, not an empty form."""
    await make_mentor(slack="U0MENTOR", code="mnt00001")
    opp = await make_opportunity()
    await make_shift(opp.id, start_in_hours=24)

    await _interact(client, _view_payload("U0MENTOR", opp.id))

    (view,) = capture_modal
    assert "submit" not in view
    assert "Mentors don't sign up" in _modal_text(view)


async def test_modal_for_a_continuous_opportunity_is_an_hours_form(
    client, capture_modal, make_student, make_opportunity
):
    """No shifts to pick, so the modal skips straight to logging hours."""
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity(is_continuous=True)

    await _interact(client, _view_payload("U0STU", opp.id))

    (view,) = capture_modal
    assert view["callback_id"] == "opportunity_log_hours"
    assert view["private_metadata"] == str(opp.id)
    assert view["submit"]["text"] == "Log hours"
    block_ids = {b.get("block_id") for b in view["blocks"]}
    assert {"hours", "report"} <= block_ids


async def test_modal_tells_an_unlinked_user_why(
    client, capture_modal, make_opportunity, make_shift
):
    opp = await make_opportunity()
    await make_shift(opp.id, start_in_hours=24)

    await _interact(client, _view_payload("U0NOBODY", opp.id))

    (view,) = capture_modal
    assert "submit" not in view
    assert "isn't linked" in _modal_text(view)


async def test_submitting_the_modal_signs_the_student_up(
    client, db, make_student, make_opportunity, make_shift
):
    student = await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity()
    shift = await make_shift(opp.id, start_in_hours=24)

    resp = await _interact(client, _submit_payload("U0STU", opp.id, shift.id))

    assert resp.status_code == 200
    assert "response_action" not in resp.text  # no errors -> Slack closes the modal
    signup = (
        await db.execute(select(Signup).where(Signup.shift_id == shift.id))
    ).scalars().first()
    assert signup is not None and signup.status == SignupStatus.signed_up


async def test_submitting_a_full_shift_keeps_the_modal_open_with_the_reason(
    client, db, make_student, make_opportunity, make_shift
):
    """Capacity is enforced by the same signup_student the web portal uses, and the
    modal stays open so they can pick a different shift."""
    student = await make_student(slack="U0STU", code="stu00001")
    other = await make_student(name="Other", slack="U0OTH", code="oth00001")
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=1, start_in_hours=24)
    db.add(Signup(shift_id=shift.id, student_id=other.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await _interact(client, _submit_payload("U0STU", opp.id, shift.id))

    assert resp.json()["response_action"] == "errors"
    assert "full" in resp.json()["errors"]["shift"].lower()
    mine = (
        await db.execute(select(Signup).where(Signup.student_id == student.id))
    ).scalars().first()
    assert mine is None


async def test_submitting_a_shift_from_another_opportunity_is_rejected(
    client, make_student, make_opportunity, make_shift
):
    """private_metadata is attacker-controlled input like any other form field."""
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity()
    other_opp = await make_opportunity(name="Other")
    foreign = await make_shift(other_opp.id, start_in_hours=24)

    resp = await _interact(client, _submit_payload("U0STU", opp.id, foreign.id))

    assert resp.json()["response_action"] == "errors"


async def test_submitting_without_picking_a_shift_errors(
    client, make_student, make_opportunity
):
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity()

    resp = await _interact(client, _submit_payload("U0STU", opp.id, None))

    assert resp.json()["response_action"] == "errors"


def _log_hours_payload(slack_id: str, opp_id: int, hours: str, report: str | None = None) -> dict:
    return {
        "type": "view_submission",
        "user": {"id": slack_id},
        "view": {
            "callback_id": "opportunity_log_hours",
            "private_metadata": str(opp_id),
            "state": {"values": {
                "hours": {"value": {"value": hours}},
                "report": {"value": {"value": report}},
            }},
        },
    }


async def test_submitting_the_log_hours_modal_creates_a_pending_submission(
    client, db, hush_slack, make_student, make_opportunity
):
    """Continuous-opportunity counterpart to test_view_submission_logs_adjusted_hours —
    same `submit_opportunity_hours` path the web log-hours form uses, just reached via
    the announcement's modal instead."""
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity(is_continuous=True)

    resp = await _interact(client, _log_hours_payload("U0STU", opp.id, "2.5", "Sorted donations"))

    assert resp.status_code == 200
    sub = (await db.execute(select(HourSubmission))).scalars().first()
    assert sub is not None
    assert sub.opportunity_id == opp.id and sub.shift_id is None
    assert sub.hours == 2.5 and sub.report == "Sorted donations"
    assert sub.status == SubmissionStatus.pending


async def test_submitting_the_log_hours_modal_with_bad_hours_returns_errors(
    client, db, make_student, make_opportunity
):
    await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity(is_continuous=True)

    resp = await _interact(client, _log_hours_payload("U0STU", opp.id, "not-a-number"))

    assert resp.json().get("response_action") == "errors"
    assert (await db.execute(select(HourSubmission))).scalars().first() is None


async def test_modal_carries_a_per_person_link_to_the_full_page(
    client, capture_modal, make_student, make_opportunity, make_shift
):
    """The modal links out for what it can't hold — the roster, cancelling a signup.
    Safe as a magic link here (unlike in the announcement it came from) because a modal
    is opened by and shown to exactly one person."""
    student = await make_student(slack="U0STU", code="stu00001")
    opp = await make_opportunity()
    await make_shift(opp.id, start_in_hours=24)

    await _interact(client, _view_payload("U0STU", opp.id))

    (view,) = capture_modal
    (payload,) = magic_link_payloads(_modal_text(view))
    assert payload["member_code"] == student.member_code
    assert payload["return_to"].endswith(f"/opportunities/{opp.id}")


async def test_mentor_modal_links_as_the_mentor_themselves(
    client, capture_modal, make_mentor, make_opportunity, make_shift
):
    mentor = await make_mentor(slack="U0MENTOR", code="mnt00001")
    opp = await make_opportunity()
    await make_shift(opp.id, start_in_hours=24)

    await _interact(client, _view_payload("U0MENTOR", opp.id))

    (view,) = capture_modal
    (payload,) = magic_link_payloads(_modal_text(view))
    assert payload["member_code"] == mentor.member_code


async def test_unlinked_user_gets_no_link(client, capture_modal, make_opportunity, make_shift):
    """No member to sign in as — offering a link would be a dead end, and there's no
    one to mint it for."""
    opp = await make_opportunity()
    await make_shift(opp.id, start_in_hours=24)

    await _interact(client, _view_payload("U0NOBODY", opp.id))

    (view,) = capture_modal
    assert magic_link_payloads(_modal_text(view)) == []
    assert "/sso/link" not in _modal_text(view)
