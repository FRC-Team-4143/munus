import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.config import settings
from app.models import HourSubmission, StudentLevel, SubmissionStatus


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
    assert "3.0 / 5.0" in resp.text
    assert "still needed" in resp.text


async def test_vhours_unlinked_user(client):
    resp = await _post_vhours(client, "U0NOBODY")
    assert resp.status_code == 200
    assert "isn't linked" in resp.text


async def test_vhours_bad_signature_rejected(client):
    body = urlencode({"command": "/vhours", "user_id": "U0STUDENT", "text": ""})
    headers = _signed_headers(body)
    headers["X-Slack-Signature"] = "v0=deadbeef"
    resp = await client.post("/slack/command", content=body, headers=headers)
    assert resp.status_code == 403
