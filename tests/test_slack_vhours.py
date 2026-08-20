import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.config import settings
from app.models import HourSubmission, StudentLevel, SubmissionStatus
from tests.conftest import magic_link_payloads


def _signed_headers(body: str) -> dict:
    ts = str(int(time.time()))
    basestring = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), basestring.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _post_vhours(client, user_id: str):
    body = urlencode({"command": "/vhours", "user_id": user_id, "text": ""})
    return await client.post("/slack/command", content=body, headers=_signed_headers(body))


async def test_vhours_for_linked_student(client, db, make_student):
    student = await make_student(level=StudentLevel.freshman, slack="U0STUDENT")
    db.add(HourSubmission(student_id=student.id, hours=3.0, status=SubmissionStatus.approved))
    await db.commit()

    resp = await _post_vhours(client, "U0STUDENT")
    assert resp.status_code == 200
    # Freshman requirement default is 5.0; 3 approved hrs -> not yet met.
    assert "3.00 / 5.00" in resp.text
    assert "still needed" in resp.text
    # Ephemeral response carries a one-tap magic link to the dashboard. Signed rather
    # than keyed on a bare member_code, so the tap needs no Approve/Deny round trip —
    # safe here precisely because an ephemeral reply is visible to this student alone.
    assert any(
        p["member_code"] == student.member_code for p in magic_link_payloads(resp.text)
    )


async def test_vhours_lists_only_upcoming_shifts(client, db, make_student, make_opportunity, make_shift):
    from app.models import Signup, SignupStatus

    student = await make_student(slack="U0STUDENT")
    future_opp = await make_opportunity(name="Future Fair")
    past_opp = await make_opportunity(name="Past Picnic")
    future = await make_shift(future_opp.id, start_in_hours=48)   # -> listed
    past = await make_shift(past_opp.id, start_in_hours=-48)      # ended -> not listed
    db.add_all([
        Signup(shift_id=future.id, student_id=student.id, status=SignupStatus.signed_up),
        Signup(shift_id=past.id, student_id=student.id, status=SignupStatus.signed_up),
    ])
    await db.commit()

    resp = await _post_vhours(client, "U0STUDENT")
    assert resp.status_code == 200
    assert "Upcoming shifts:" in resp.text
    assert "Future Fair" in resp.text
    assert "Past Picnic" not in resp.text


async def test_vhours_unlinked_user(client):
    resp = await _post_vhours(client, "U0NOBODY")
    assert resp.status_code == 200
    assert "isn't linked" in resp.text


async def test_vhours_for_mentor_links_to_opportunities(client, make_mentor):
    mentor = await make_mentor(slack="U0MENTOR", code="deadbeef")

    resp = await _post_vhours(client, "U0MENTOR")
    assert resp.status_code == 200
    assert "nothing to report" in resp.text
    # One-tap magic link for the mentor themselves, pointed at the opportunities list
    # rather than a student dashboard.
    assert any(
        p["member_code"] == mentor.member_code and p["return_to"].endswith("/opportunities")
        for p in magic_link_payloads(resp.text)
    )


async def test_vhours_bad_signature_rejected(client):
    body = urlencode({"command": "/vhours", "user_id": "U0STUDENT", "text": ""})
    headers = _signed_headers(body)
    headers["X-Slack-Signature"] = "v0=deadbeef"
    resp = await client.post("/slack/command", content=body, headers=headers)
    assert resp.status_code == 403


# ── /munus — bare one-tap link, no stats (mirrors /tempus, /legion) ────────────

async def _post_munus(client, user_id: str):
    body = urlencode({"command": "/munus", "user_id": user_id})
    return await client.post("/slack/command", content=body, headers=_signed_headers(body))


async def test_munus_command_links_a_student_to_me(client, make_student):
    student = await make_student(level=StudentLevel.freshman, slack="U0STUDENT")

    resp = await _post_munus(client, "U0STUDENT")

    assert resp.status_code == 200
    assert any(
        p["member_code"] == student.member_code and p["return_to"].endswith("/me")
        for p in magic_link_payloads(resp.text)
    )


async def test_munus_command_links_a_mentor_to_opportunities(client, make_mentor):
    """Munus's /me is student-only — a mentor gets /opportunities instead, matching
    the home-page launcher's own tile for mentors (legion/app/services/home.py)."""
    mentor = await make_mentor(slack="U0MENTOR", code="deadbeef")

    resp = await _post_munus(client, "U0MENTOR")

    assert resp.status_code == 200
    assert any(
        p["member_code"] == mentor.member_code and p["return_to"].endswith("/opportunities")
        for p in magic_link_payloads(resp.text)
    )


async def test_munus_command_tells_an_unlinked_user_why(client):
    resp = await _post_munus(client, "U0NOBODY")

    assert resp.status_code == 200
    assert "isn't linked" in resp.text
    assert magic_link_payloads(resp.text) == []


async def test_munus_command_bad_signature_rejected(client):
    body = urlencode({"command": "/munus", "user_id": "U0STUDENT"})
    headers = _signed_headers(body)
    headers["X-Slack-Signature"] = "v0=deadbeef"
    resp = await client.post("/slack/command", content=body, headers=headers)
    assert resp.status_code == 403
