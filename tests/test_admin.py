"""Smoke tests for the admin UI (Legion SSO auth + template rendering)."""
import pytest
from sqlalchemy import select

from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


async def _login(client, **cookie_kwargs):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(**cookie_kwargs))


async def test_admin_requires_auth(client):
    resp = await client.get("/admin/roster", follow_redirects=False)
    assert resp.status_code == 303  # redirect to Legion sign-in
    assert "sso/authorize" in resp.headers["location"]


async def test_admin_forbidden_without_group(client):
    await _login(client, groups=())
    resp = await client.get("/admin/roster")
    assert resp.status_code == 403


async def test_admin_sends_a_magic_link_identity_to_stepup_rather_than_403(client):
    """A magic-link cookie is deliberately non-privileged (Legion strips `groups` and
    marks it `via: "link"`), so it can never reach /admin. Bounce it to `/sso/stepup`
    (a fresh Approve/Deny that re-mints the cookie *with* groups, landing back here)
    instead of a dead-end 403 — and not `/sso/authorize`, which would just bounce the
    link cookie straight back and loop."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=(), via="link"))

    resp = await client.get("/admin/roster", follow_redirects=False)

    assert resp.status_code == 303
    assert "/sso/stepup?app=munus" in resp.headers["location"]


@pytest.mark.parametrize("path", [
    "/admin", "/admin/opportunities", "/admin/submissions", "/admin/roster",
    "/admin/report", "/admin/audit", "/admin/backup", "/admin/settings",
])
async def test_admin_pages_render(client, path):
    await _login(client)
    resp = await client.get(path)
    assert resp.status_code == 200


async def test_admin_page_has_favicon(client):
    await _login(client)
    resp = await client.get("/admin")
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in resp.text


async def test_opportunity_purge_requires_archived_then_cascades(
    client, db, make_student, make_opportunity, make_shift
):
    from app.models import (
        HourSubmission, Opportunity, Shift, Signup, SignupStatus, SubmissionStatus,
    )

    await _login(client)
    student = await make_student(code="opp00001")
    opp = await make_opportunity()
    shift = await make_shift(opp.id)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(
        student_id=student.id, opportunity_id=opp.id, shift_id=shift.id,
        hours=4.0, status=SubmissionStatus.approved,
    ))
    await db.commit()
    oid, shid, sid = opp.id, shift.id, student.id

    async def _exists(model, **filters):
        q = select(model)
        for k, v in filters.items():
            q = q.where(getattr(model, k) == v)
        return (await db.execute(q)).scalars().first() is not None

    # Active opportunity: purge is refused (archive-gated).
    r = await client.post(f"/admin/opportunities/{oid}/purge", follow_redirects=False)
    assert r.status_code == 303
    assert await _exists(Opportunity, id=oid)

    # Archive, then purge -> opportunity + shift + signup + logged hours are all gone.
    await client.post(f"/admin/opportunities/{oid}/archive")  # toggles is_active off
    r = await client.post(f"/admin/opportunities/{oid}/purge", follow_redirects=False)
    assert r.status_code == 303
    assert not await _exists(Opportunity, id=oid)
    assert not await _exists(Shift, id=shid)
    assert not await _exists(Signup, shift_id=shid)
    assert not await _exists(HourSubmission, student_id=sid)


async def test_admin_add_manual_hours(client, db, make_student, make_opportunity):
    from app.models import HourSubmission, StudentLevel, SubmissionStatus
    from app.services.reports import student_progress_report

    await _login(client)
    student = await make_student(code="man00001", level=StudentLevel.freshman)  # required 5
    opp = await make_opportunity(name="Preseason Build")

    # The form renders with the opportunity dropdown populated.
    page = await client.get("/admin/submissions/new")
    assert page.status_code == 200
    assert "Preseason Build" in page.text

    # Posting approved hours creates an approved, reviewed submission.
    resp = await client.post("/admin/submissions/new", data={
        "student_id": str(student.id),
        "hours": "12",
        "submitted_on": "2026-07-01",
        "opportunity_id": str(opp.id),
        "report": "Preseason build sessions",
        "status": "approved",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "added=1" in resp.headers["location"]

    sub = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == student.id))
    ).scalars().first()
    assert sub is not None
    assert sub.status == SubmissionStatus.approved
    assert sub.hours == 12.0
    assert sub.opportunity_id == opp.id
    assert sub.reviewed_at is not None

    # It counts toward the student's approved total in the report.
    rows = await student_progress_report(db)
    assert rows[0]["approved"] == 12.0


async def test_submission_edit_page_and_delete(
    client, db, make_student, make_mentor, make_opportunity, make_shift
):
    from app.models import HourSubmission, Signup, SignupStatus
    from app.services.submissions import submit_shift_hours
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload as _selin

    await _login(client)
    mentor = await make_mentor(slack="U0REV")
    student = await make_student(code="sub00001")
    opp = await make_opportunity(reviewer_mentor_id=mentor.id)
    shift = await make_shift(opp.id, length_hours=3)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()
    signup = (
        await db.execute(
            _select(Signup).options(_selin(Signup.shift)).where(Signup.student_id == student.id)
        )
    ).scalars().first()
    sub = await submit_shift_hours(db, signup, 3.0, "did stuff")  # a shift-linked submission

    # Edit page must render (regression: shift was not eager-loaded → 500).
    page = await client.get(f"/admin/submissions/{sub.id}/edit")
    assert page.status_code == 200

    # Delete removes it.
    r = await client.post(f"/admin/submissions/{sub.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert (await db.execute(_select(HourSubmission).where(HourSubmission.id == sub.id))).scalars().first() is None


async def test_opportunity_notify_dms_upcoming_signups(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    student = await make_student(slack="U0STU")
    opp = await make_opportunity(name="Food Drive", location="Community Center", attire="Team polo")
    upcoming = await make_shift(opp.id, start_in_hours=24)
    past = await make_shift(opp.id, start_in_hours=-48)  # ended → not included
    db.add(Signup(shift_id=upcoming.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=past.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(f"/admin/opportunities/{opp.id}/notify", follow_redirects=False)
    assert resp.status_code == 303
    assert "notified=1" in resp.headers["location"]
    # One grouped DM to the student, referencing the opportunity + location.
    assert len(calls) == 1 and calls[0][0] == "U0STU"
    assert "Food Drive" in calls[0][1]
    assert "Community Center" in calls[0][1]
    assert "Team polo" in calls[0][1]  # attire included


async def test_opportunity_message_dms_upcoming_signups_custom_text(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    student = await make_student(slack="U0STU")
    opp = await make_opportunity(name="Food Drive")
    upcoming = await make_shift(opp.id, start_in_hours=24)
    past = await make_shift(opp.id, start_in_hours=-48)  # ended → not included
    db.add(Signup(shift_id=upcoming.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=past.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(
        f"/admin/opportunities/{opp.id}/message",
        data={"message": "Bring gloves tomorrow!"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "messaged=1" in resp.headers["location"]
    assert len(calls) == 1
    # No reviewer configured on the opportunity -> signed with the sender only.
    assert calls[0] == ("U0STU", "Bring gloves tomorrow!\n\n*Sent by:* Test Admin")


async def test_opportunity_message_signs_with_mentioned_sender_and_approver(
    client, db, monkeypatch, make_student, make_mentor, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client, slack_user_id="U0ADMIN")
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(name="Coach Ray", slack="U0MENTOR")
    opp = await make_opportunity(name="Food Drive", reviewer_mentor_id=mentor.id)
    shift = await make_shift(opp.id, start_in_hours=24)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(
        f"/admin/opportunities/{opp.id}/message", data={"message": "Bring gloves!"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert calls[0] == (
        "U0STU", "Bring gloves!\n\n*Sent by:* <@U0ADMIN>\n*Approver:* <@U0MENTOR>"
    )


async def test_opportunity_message_rejects_blank_message(client, make_opportunity):
    await _login(client)
    opp = await make_opportunity(name="Food Drive")
    resp = await client.post(
        f"/admin/opportunities/{opp.id}/message", data={"message": "   "}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


async def test_shift_message_dms_only_that_shifts_signups(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    on_shift = await make_student(name="On Shift", code="on000001", slack="U0ONSHIFT")
    other_shift_student = await make_student(name="Other Shift", code="ot000001", slack="U0OTHER")
    opp = await make_opportunity(name="Food Drive")
    shift = await make_shift(opp.id, start_in_hours=24)
    other_shift = await make_shift(opp.id, start_in_hours=48)
    db.add(Signup(shift_id=shift.id, student_id=on_shift.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=other_shift.id, student_id=other_shift_student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(
        f"/admin/shifts/{shift.id}/message", data={"message": "Meet at the loading dock"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert f"/admin/opportunities/{opp.id}/edit" in resp.headers["location"]
    assert "messaged=1" in resp.headers["location"]
    assert len(calls) == 1
    # No reviewer configured -> signed with the sender only.
    assert calls[0] == ("U0ONSHIFT", "Meet at the loading dock\n\n*Sent by:* Test Admin")


async def test_shift_message_uses_shifts_own_reviewer_override(
    client, db, monkeypatch, make_student, make_mentor, make_opportunity, make_shift
):
    """A shift-specific approver override should win over the opportunity's default
    approver, mirroring resolve_reviewer_id's own precedence."""
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    student = await make_student(slack="U0STU")
    default_mentor = await make_mentor(name="Default Mentor", slack="U0DEFAULT")
    override_mentor = await make_mentor(name="Override Mentor", slack=None, code="ov000001")
    opp = await make_opportunity(name="Food Drive", reviewer_mentor_id=default_mentor.id)
    shift = await make_shift(opp.id, start_in_hours=24)
    shift.reviewer_mentor_id = override_mentor.id
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(
        f"/admin/shifts/{shift.id}/message", data={"message": "Heads up"}, follow_redirects=False
    )
    assert resp.status_code == 303
    # Override mentor has no linked Slack account -> falls back to plain name.
    assert calls[0] == ("U0STU", "Heads up\n\n*Sent by:* Test Admin\n*Approver:* Override Mentor")


async def test_opportunities_list_hides_notify_and_message_for_continuous(client, make_opportunity):
    await _login(client)
    await make_opportunity(name="Ongoing Restock", is_continuous=True)
    page = await client.get("/admin/opportunities")
    assert page.status_code == 200
    assert "Ongoing Restock" in page.text
    body = page.text
    start = body.index("Ongoing Restock")
    end = body.index("</tr>", start)
    row = body[start:end]
    assert "/notify" not in row
    assert "/message" not in row


def test_manager_allowed_excludes_purge():
    """Managers may create/manage opportunities and shifts, but the irreversible purge is
    admin-only — it deletes hour submissions, so it doesn't belong in the manager scope."""
    from app.routers.admin import _manager_allowed

    assert _manager_allowed("/admin/opportunities") is True
    assert _manager_allowed("/admin/opportunities/5/edit") is True
    assert _manager_allowed("/admin/opportunities/5/archive") is True
    assert _manager_allowed("/admin/shifts/3/edit") is True
    assert _manager_allowed("/admin/shifts/3/signups/7/remove") is True
    # Purge is excluded, with or without a trailing slash.
    assert _manager_allowed("/admin/opportunities/5/purge") is False
    assert _manager_allowed("/admin/opportunities/5/purge/") is False
    # Unrelated admin sections remain manager-forbidden.
    assert _manager_allowed("/admin/submissions") is False


async def test_decision_redirect_ignores_referer(
    client, db, make_student, make_opportunity, hush_slack
):
    """The quick approve/reject redirect must go to a fixed path, never reflect the
    untrusted Referer header into the Location."""
    from app.models import HourSubmission, SubmissionStatus

    await _login(client)
    student = await make_student()
    opp = await make_opportunity()
    db.add(HourSubmission(
        student_id=student.id, opportunity_id=opp.id, hours=3.0,
        status=SubmissionStatus.pending,
    ))
    await db.commit()
    sub = (await db.execute(select(HourSubmission))).scalars().first()

    resp = await client.post(
        f"/admin/submissions/{sub.id}/decision",
        data={"decision": "approve"},
        headers={"referer": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/submissions"


async def test_manager_role_scoped_to_opportunities(client):
    await _login(client, groups=("munus-manager",))

    # Can view + create opportunities.
    assert (await client.get("/admin/opportunities")).status_code == 200
    cr = await client.post("/admin/opportunities", data={"name": "Mgr Opp"}, follow_redirects=False)
    assert cr.status_code == 303 and "/admin/opportunities/" in cr.headers["location"]

    # ...but NOT the irreversible purge, which permanently deletes an opportunity and
    # every hour submission logged against it — that stays full-admin-only even though it
    # lives under /admin/opportunities/. The reversible archive toggle is still allowed.
    oid = cr.headers["location"].split("/admin/opportunities/")[1].split("/")[0]
    pr = await client.post(f"/admin/opportunities/{oid}/purge", follow_redirects=False)
    assert pr.status_code == 403 and "No Access" in pr.text
    ar = await client.post(f"/admin/opportunities/{oid}/archive", follow_redirects=False)
    assert ar.status_code == 303

    # Can also view the dashboard and season report — read-only visibility, not
    # full admin.
    assert (await client.get("/admin")).status_code == 200
    assert (await client.get("/admin/report")).status_code == 200

    # Blocked from every other admin-only section — stays in the admin shell with a
    # blur-blocked "No Access" page rather than being silently redirected away
    # (regression test: it used to 303 to Opportunities with no explanation).
    for path in ("/admin/roster", "/admin/submissions", "/admin/settings", "/admin/backup"):
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code == 403, path
        assert "No Access" in resp.text, path

    # Sidebar shows every section to every tier, regardless of access.
    page = await client.get("/admin/opportunities")
    assert "/admin/roster" in page.text
    assert "/admin/backup" in page.text
    assert "/admin/opportunities" in page.text


async def test_admin_sidebar_shows_legion_link_when_configured(client):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = "https://legion.example.org"
        await _login(client)
        resp = await client.get("/admin")
        assert 'href="https://legion.example.org"' in resp.text
    finally:
        settings.legion_base_url = original


async def test_admin_sidebar_hides_legion_link_when_unconfigured(client):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = ""
        await _login(client)
        resp = await client.get("/admin")
        assert ">Legion</a>" not in resp.text
    finally:
        settings.legion_base_url = original


async def test_report_notify_dms_slack_linked_students(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    linked = await make_student(code="rn000001", slack="U0STU")
    await make_student(code="rn000002")  # no Slack ID -> skipped
    opp = await make_opportunity(name="Beach Cleanup")
    shift = await make_shift(opp.id, start_in_hours=24)
    db.add(Signup(shift_id=shift.id, student_id=linked.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post("/admin/report/notify", follow_redirects=False)
    assert resp.status_code == 303
    assert "notified=1" in resp.headers["location"]
    # Only the Slack-linked student is DMed, with the /vhours summary content.
    assert len(calls) == 1 and calls[0][0] == "U0STU"
    assert "Season total:" in calls[0][1]
    assert "Beach Cleanup" in calls[0][1]


async def test_report_notify_incomplete_only_dms_students_behind(
    client, db, monkeypatch, make_student
):
    import app.routers.admin as adminmod
    from app.models import HourSubmission, StudentLevel, SubmissionStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append(uid)
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    on_track = await make_student(
        name="OnTrack", code="ot000001", slack="U0MET", level=StudentLevel.freshman  # req 5
    )
    db.add(HourSubmission(student_id=on_track.id, hours=6.0, status=SubmissionStatus.approved))
    await make_student(
        name="Behind", code="bh000001", slack="U0BEHIND", level=StudentLevel.freshman
    )
    await db.commit()

    resp = await client.post("/admin/report/notify?incomplete=1", follow_redirects=False)
    assert resp.status_code == 303
    assert "notified=1" in resp.headers["location"]
    assert calls == ["U0BEHIND"]


async def test_admin_report_export_csv(client):
    await _login(client)
    resp = await client.get("/admin/report/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "Student,Member Code,Level,Opportunity,Shift,Hours,Status" in resp.text


async def test_admin_student_submissions_requires_auth(client, make_student):
    student = await make_student()
    resp = await client.get(f"/admin/students/{student.id}/submissions", follow_redirects=False)
    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_admin_student_submissions_forbidden_without_group(client, make_student):
    student = await make_student()
    await _login(client, groups=())
    resp = await client.get(f"/admin/students/{student.id}/submissions")
    assert resp.status_code == 403


async def test_admin_student_submissions_allowed_for_manager(client, make_student):
    # Part of the Report screen's name-click modal, which managers can now reach.
    student = await make_student()
    await _login(client, groups=("munus-manager",))
    resp = await client.get(f"/admin/students/{student.id}/submissions")
    assert resp.status_code == 200


async def test_admin_student_submissions_scoped_to_student(client, db, make_student, make_opportunity):
    from app.models import HourSubmission, SubmissionStatus

    student_a = await make_student(name="Ada Lovelace", code="ada00001")
    student_b = await make_student(name="Grace Hopper", code="grc00001")
    opp_a = await make_opportunity(name="Beach Cleanup")
    opp_b = await make_opportunity(name="Food Drive")
    db.add_all([
        HourSubmission(
            student_id=student_a.id, opportunity_id=opp_a.id, hours=2.5,
            status=SubmissionStatus.approved,
        ),
        HourSubmission(
            student_id=student_a.id, opportunity_id=opp_a.id, hours=1.25,
            status=SubmissionStatus.rejected, review_note="Didn't match the shift time.",
        ),
        HourSubmission(
            student_id=student_b.id, opportunity_id=opp_b.id, hours=3.0,
            status=SubmissionStatus.approved,
        ),
    ])
    await db.commit()

    await _login(client)
    resp = await client.get(f"/admin/students/{student_a.id}/submissions")
    assert resp.status_code == 200
    assert "Beach Cleanup" in resp.text
    assert "2.50" in resp.text
    assert "1.25" in resp.text
    assert "Didn&#39;t match the shift time." in resp.text or "Didn't match the shift time." in resp.text
    # Student B's data must not leak into student A's fragment.
    assert "Food Drive" not in resp.text
    assert "3.00" not in resp.text


async def test_admin_student_submissions_empty(client, make_student):
    student = await make_student()
    await _login(client)
    resp = await client.get(f"/admin/students/{student.id}/submissions")
    assert resp.status_code == 200
    assert "No submissions yet." in resp.text


async def test_admin_student_submissions_404_unknown_student(client):
    await _login(client)
    resp = await client.get("/admin/students/999999/submissions")
    assert resp.status_code == 404


async def test_admin_create_opportunity_and_shift(client):
    await _login(client)
    # Create opportunity -> redirects to its edit page.
    resp = await client.post("/admin/opportunities", data={
        "name": "Park Cleanup", "description": "Pick up litter",
        "location": "River Park", "attire": "Old clothes", "contact": "Ms. Lee",
    })
    assert resp.status_code == 303
    edit_url = resp.headers["location"]

    edit = await client.get(edit_url)
    assert edit.status_code == 200
    assert "Park Cleanup" in edit.text

    # Add a shift to it.
    opp_id = edit_url.rstrip("/edit").split("/")[-1]
    resp = await client.post(f"/admin/opportunities/{opp_id}/shifts", data={
        "date": "2026-08-01", "start_time": "09:00", "end_time": "12:00",
        "capacity": "6", "notes": "Bring gloves",
    })
    assert resp.status_code == 303


async def test_shift_create_rejects_end_before_start(client, db, make_opportunity):
    """A shift whose end is not after its start is rejected (no row created)."""
    from sqlalchemy import func
    from app.models import Shift

    await _login(client)
    opp = await make_opportunity()
    resp = await client.post(
        f"/admin/opportunities/{opp.id}/shifts",
        data={"date": "2026-08-01", "start_time": "15:00", "end_time": "14:00", "capacity": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    count = (await db.execute(select(func.count()).select_from(Shift))).scalar_one()
    assert count == 0


async def test_admin_create_continuous_opportunity_hides_shift_ui(client, db):
    from app.models import Opportunity

    await _login(client)
    resp = await client.post("/admin/opportunities", data={
        "name": "CAD Subteam", "is_continuous": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303
    edit_url = resp.headers["location"]

    opp = (await db.execute(select(Opportunity).where(Opportunity.name == "CAD Subteam"))).scalars().first()
    assert opp.is_continuous is True

    edit = await client.get(edit_url)
    assert edit.status_code == 200
    assert "ongoing activity" in edit.text.lower()
    assert "Add Shift" not in edit.text


async def test_admin_create_opportunity_defaults_to_shift_based(client, db):
    from app.models import Opportunity

    await _login(client)
    resp = await client.post("/admin/opportunities", data={"name": "Food Drive"}, follow_redirects=False)
    edit_url = resp.headers["location"]

    opp = (await db.execute(select(Opportunity).where(Opportunity.name == "Food Drive"))).scalars().first()
    assert opp.is_continuous is False

    edit = await client.get(edit_url)
    assert "Add Shift" in edit.text


async def test_admin_edit_opportunity_toggles_is_continuous(client, db, make_opportunity):
    from app.models import Opportunity

    await _login(client)
    opp = await make_opportunity(name="Outreach Committee")
    assert opp.is_continuous is False

    resp = await client.post(f"/admin/opportunities/{opp.id}/edit", data={
        "name": "Outreach Committee", "is_continuous": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303

    await db.refresh(opp)
    assert opp.is_continuous is True

    # Unchecking (the checkbox is simply absent from the posted form) reverts it.
    await client.post(f"/admin/opportunities/{opp.id}/edit", data={"name": "Outreach Committee"})
    await db.refresh(opp)
    assert opp.is_continuous is False


async def test_admin_create_required_opportunity(client, db):
    from app.models import Opportunity

    await _login(client)
    resp = await client.post("/admin/opportunities", data={
        "name": "Bag Night", "is_required": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303

    opp = (await db.execute(select(Opportunity).where(Opportunity.name == "Bag Night"))).scalars().first()
    assert opp.is_required is True


async def test_admin_create_continuous_and_required_normalizes_to_not_required(client, db):
    """is_required is only meaningful for shift-based opportunities -- the server
    normalizes it off even if both checkboxes were posted (e.g. bypassing the
    client-side JS toggle)."""
    from app.models import Opportunity

    await _login(client)
    resp = await client.post("/admin/opportunities", data={
        "name": "CAD Subteam", "is_continuous": "true", "is_required": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303

    opp = (await db.execute(select(Opportunity).where(Opportunity.name == "CAD Subteam"))).scalars().first()
    assert opp.is_continuous is True
    assert opp.is_required is False


async def test_admin_edit_opportunity_toggles_is_required(client, db, make_opportunity):
    await _login(client)
    opp = await make_opportunity(name="Bag Night")
    assert opp.is_required is False

    resp = await client.post(f"/admin/opportunities/{opp.id}/edit", data={
        "name": "Bag Night", "is_required": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303

    await db.refresh(opp)
    assert opp.is_required is True

    # Unchecking (the checkbox is simply absent from the posted form) reverts it.
    await client.post(f"/admin/opportunities/{opp.id}/edit", data={"name": "Bag Night"})
    await db.refresh(opp)
    assert opp.is_required is False


async def test_admin_edit_setting_continuous_clears_required(client, db, make_opportunity):
    await _login(client)
    opp = await make_opportunity(name="Bag Night", is_required=True)

    await client.post(f"/admin/opportunities/{opp.id}/edit", data={
        "name": "Bag Night", "is_continuous": "true", "is_required": "true",
    })
    await db.refresh(opp)
    assert opp.is_continuous is True
    assert opp.is_required is False


async def test_admin_edit_syncs_slack_announcement_when_previously_posted(client, db, make_opportunity, monkeypatch):
    """Editing an opportunity that already has a posted announcement (channel + ts on
    record) pushes the new details to that same Slack message via chat.update."""
    import app.services.opportunities as opp_module

    calls = []

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        calls.append((channel_id, ts, text))
        return True

    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)

    await _login(client)
    opp = await make_opportunity(
        name="Bag Night", announcement_channel_id="C0ANNOUNCE", announcement_ts="1699999999.000100",
    )

    resp = await client.post(f"/admin/opportunities/{opp.id}/edit", data={
        "name": "Bag Night (Rescheduled)",
    }, follow_redirects=False)
    assert resp.status_code == 303

    assert len(calls) == 1
    channel_id, ts, text = calls[0]
    assert channel_id == "C0ANNOUNCE"
    assert ts == "1699999999.000100"
    assert "Bag Night (Rescheduled)" in text


async def test_admin_edit_does_not_call_slack_when_never_announced(client, db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module

    async def fail_if_called(*a, **k):
        raise AssertionError("update_channel_message should not be called")

    monkeypatch.setattr(opp_module, "update_channel_message", fail_if_called)

    await _login(client)
    opp = await make_opportunity(name="Food Drive")

    resp = await client.post(f"/admin/opportunities/{opp.id}/edit", data={
        "name": "Food Drive (Updated)",
    }, follow_redirects=False)
    assert resp.status_code == 303


async def test_admin_archive_syncs_slack_announcement(client, db, make_opportunity, monkeypatch):
    """Archiving must update an already-posted announcement to say so — otherwise it
    keeps advertising a closed opportunity indefinitely."""
    import app.services.opportunities as opp_module

    calls = []

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        calls.append(text)
        return True

    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)

    await _login(client)
    opp = await make_opportunity(
        name="Bag Night", announcement_channel_id="C0ANNOUNCE", announcement_ts="1699999999.000100",
    )

    resp = await client.post(f"/admin/opportunities/{opp.id}/archive", follow_redirects=False)
    assert resp.status_code == 303

    assert len(calls) == 1
    assert "Archived" in calls[0]

    # Restoring syncs it back — same call, opp.is_active is just True again by then.
    resp = await client.post(f"/admin/opportunities/{opp.id}/archive", follow_redirects=False)
    assert resp.status_code == 303
    assert len(calls) == 2
    assert "Archived" not in calls[1]


async def test_admin_archive_does_not_call_slack_when_never_announced(client, db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module

    async def fail_if_called(*a, **k):
        raise AssertionError("update_channel_message should not be called")

    monkeypatch.setattr(opp_module, "update_channel_message", fail_if_called)

    await _login(client)
    opp = await make_opportunity(name="Food Drive")

    resp = await client.post(f"/admin/opportunities/{opp.id}/archive", follow_redirects=False)
    assert resp.status_code == 303


async def test_admin_announce_now_posts_and_persists(client, db, make_opportunity, monkeypatch):
    """The manual "Post announcement now" action is how an opportunity that missed its
    normal trigger (e.g. SLACK_ANNOUNCE_CHANNEL was blank at creation) can still get
    announced — update_announcement only ever syncs an *existing* announcement."""
    import app.services.opportunities as opp_module
    from app.config import settings

    async def fake_post_to_channel(channel_id, text, blocks=None, automated=True):
        return "1700000000.000200"

    monkeypatch.setattr(opp_module, "post_to_channel", fake_post_to_channel)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = "C0ANNOUNCE"
    try:
        await _login(client)
        opp = await make_opportunity(name="CAD Subteam", is_continuous=True)

        resp = await client.post(
            f"/admin/opportunities/{opp.id}/announce", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?announced=1")

        await db.refresh(opp)
        assert opp.announcement_channel_id == "C0ANNOUNCE"
        assert opp.announcement_ts == "1700000000.000200"
    finally:
        settings.slack_announce_channel = original


async def test_admin_announce_now_errors_without_channel_configured(client, db, make_opportunity):
    from app.config import settings

    original = settings.slack_announce_channel
    settings.slack_announce_channel = ""
    try:
        await _login(client)
        opp = await make_opportunity(name="CAD Subteam", is_continuous=True)

        resp = await client.post(
            f"/admin/opportunities/{opp.id}/announce", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        await db.refresh(opp)
        assert opp.announcement_channel_id is None
    finally:
        settings.slack_announce_channel = original


async def test_admin_announce_now_noops_if_already_announced(client, db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module
    from app.config import settings

    async def fail_if_called(*a, **k):
        raise AssertionError("post_to_channel should not be called")

    monkeypatch.setattr(opp_module, "post_to_channel", fail_if_called)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = "C0ANNOUNCE"
    try:
        await _login(client)
        opp = await make_opportunity(
            name="Bag Night", announcement_channel_id="C0OLD", announcement_ts="1699999999.000100",
        )

        resp = await client.post(
            f"/admin/opportunities/{opp.id}/announce", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "announced" not in resp.headers["location"]
    finally:
        settings.slack_announce_channel = original


async def test_first_shift_announcement_includes_date(client, db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module
    from app.config import settings

    calls = []

    async def fake_post_to_channel(channel_id, text, blocks=None, automated=True):
        calls.append(text)
        return "1699999999.000100"

    monkeypatch.setattr(opp_module, "post_to_channel", fake_post_to_channel)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = "C0ANNOUNCE"
    try:
        await _login(client)
        opp = await make_opportunity(name="Bag Night")

        resp = await client.post(f"/admin/opportunities/{opp.id}/shifts", data={
            "date": "2026-11-12", "start_time": "18:00", "end_time": "21:00", "capacity": "10",
        }, follow_redirects=False)
        assert resp.status_code == 303

        assert len(calls) == 1
        assert "📅" in calls[0] and "Nov" in calls[0]
        await db.refresh(opp)
        assert opp.announcement_channel_id == "C0ANNOUNCE"
        assert opp.announcement_ts == "1699999999.000100"
    finally:
        settings.slack_announce_channel = original


async def test_second_shift_updates_existing_announcement_instead_of_reposting(
    client, db, make_opportunity, monkeypatch
):
    """Only the FIRST shift announces fresh; a later shift updates that same message
    (its date span may now be wider) instead of posting a duplicate announcement."""
    import app.services.opportunities as opp_module
    from app.config import settings

    post_calls = []
    update_calls = []

    async def fake_post_to_channel(channel_id, text, blocks=None, automated=True):
        post_calls.append(text)
        return "1699999999.000100"

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        update_calls.append(text)
        return True

    monkeypatch.setattr(opp_module, "post_to_channel", fake_post_to_channel)
    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = "C0ANNOUNCE"
    try:
        await _login(client)
        opp = await make_opportunity(name="Bag Night")

        await client.post(f"/admin/opportunities/{opp.id}/shifts", data={
            "date": "2026-11-12", "start_time": "18:00", "end_time": "21:00", "capacity": "10",
        })
        assert len(post_calls) == 1
        assert len(update_calls) == 0

        await client.post(f"/admin/opportunities/{opp.id}/shifts", data={
            "date": "2026-11-14", "start_time": "06:00", "end_time": "11:30", "capacity": "20",
        })
        assert len(post_calls) == 1  # still just the one original post
        assert len(update_calls) == 1
        assert "Nov" in update_calls[0]
    finally:
        settings.slack_announce_channel = original


async def test_shift_edit_updates_announcement_date_span(client, db, make_opportunity, make_shift, monkeypatch):
    import app.services.opportunities as opp_module

    calls = []

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        calls.append(text)
        return True

    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)

    await _login(client)
    opp = await make_opportunity(
        name="Bag Night", announcement_channel_id="C0ANNOUNCE", announcement_ts="1699999999.000100",
    )
    shift = await make_shift(opp.id)

    resp = await client.post(f"/admin/shifts/{shift.id}/edit", data={
        "date": "2026-12-25", "start_time": "09:00", "end_time": "12:00", "capacity": "0",
    }, follow_redirects=False)
    assert resp.status_code == 303

    assert len(calls) == 1
    assert "Dec" in calls[0]


async def test_shift_delete_updates_announcement(client, db, make_opportunity, make_shift, monkeypatch):
    import app.services.opportunities as opp_module

    calls = []

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        calls.append(text)
        return True

    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)

    await _login(client)
    opp = await make_opportunity(
        name="Bag Night", announcement_channel_id="C0ANNOUNCE", announcement_ts="1699999999.000100",
    )
    shift = await make_shift(opp.id)

    resp = await client.post(f"/admin/shifts/{shift.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert len(calls) == 1


async def test_admin_report_surfaces_missing_required_opportunity(client, db, make_student, make_opportunity, make_shift):
    """Missing-required-opportunity tracking is a season-progress KPI — it's shown on
    the on-screen report table but isn't part of the CSV export's combined
    submission-log + total shape (see test_hours_export.py)."""
    from app.models import StudentLevel

    await _login(client)
    await make_student(name="Ada Lovelace", level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(opp.id, start_in_hours=24)

    report = await client.get("/admin/report")
    assert report.status_code == 200
    assert 'title="Missing: Bag Night"' in report.text


async def test_admin_edit_shift_updates_fields(client, db, make_opportunity, make_shift):
    from datetime import datetime
    from app.utils import local_to_utc

    await _login(client)
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=2)

    resp = await client.post(
        f"/admin/shifts/{shift.id}/edit",
        data={
            "date": "2026-09-01", "start_time": "09:00", "end_time": "13:00",
            "capacity": "10", "notes": "Bring water",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/opportunities/{opp.id}/edit"

    await db.refresh(shift)
    assert shift.capacity == 10
    assert shift.notes == "Bring water"
    assert shift.start_time == local_to_utc(datetime(2026, 9, 1, 9, 0))
    assert shift.end_time == local_to_utc(datetime(2026, 9, 1, 13, 0))


async def test_admin_edit_shift_rejects_end_before_start(client, db, make_opportunity, make_shift):
    """Editing a shift is held to the same start/end ordering as creating one, and
    leaves the existing row untouched on rejection."""
    await _login(client)
    opp = await make_opportunity()
    shift = await make_shift(opp.id, start_in_hours=24, length_hours=3)
    original_start = shift.start_time

    resp = await client.post(
        f"/admin/shifts/{shift.id}/edit",
        data={"date": "2026-08-01", "start_time": "15:00", "end_time": "14:00", "capacity": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    await db.refresh(shift)
    assert shift.start_time == original_start


async def test_admin_edit_shift_requires_auth(client, db, make_opportunity, make_shift):
    opp = await make_opportunity()
    shift = await make_shift(opp.id)
    resp = await client.post(
        f"/admin/shifts/{shift.id}/edit",
        data={"date": "2026-09-01", "start_time": "09:00", "end_time": "13:00", "capacity": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_opportunity_edit_page_shows_edit_shift_modal(client, db, make_opportunity, make_shift):
    """The shift's current values are pre-filled into its edit modal."""
    await _login(client)
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=4)

    resp = await client.get(f"/admin/opportunities/{opp.id}/edit")
    assert resp.status_code == 200
    assert f'id="editShift{shift.id}"' in resp.text
    assert f'value="{shift.capacity}"' in resp.text


async def test_opportunity_edit_page_shows_shift_roster(
    client, db, make_student, make_opportunity, make_shift
):
    """The per-shift roster toggle lists the signed-up students by name, and a
    cancelled signup doesn't appear."""
    from app.models import Signup, SignupStatus

    await _login(client)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    cancelled = await make_student(name="Grace Hopper", code="gra00001")
    opp = await make_opportunity()
    shift = await make_shift(opp.id)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=shift.id, student_id=cancelled.id, status=SignupStatus.cancelled))
    await db.commit()

    resp = await client.get(f"/admin/opportunities/{opp.id}/edit")
    assert resp.status_code == 200
    assert f'id="roster{shift.id}"' in resp.text
    assert "Ada Lovelace" in resp.text
    assert "Grace Hopper" not in resp.text


async def test_admin_remove_signup_cancels_and_notifies_student(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    student = await make_student(name="Ada Lovelace", slack="U0STU")
    opp = await make_opportunity(name="Food Drive")
    shift = await make_shift(opp.id)
    signup = Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up)
    db.add(signup)
    await db.commit()
    await db.refresh(signup)

    resp = await client.post(
        f"/admin/shifts/{shift.id}/signups/{signup.id}/remove", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/opportunities/{opp.id}/edit"

    await db.refresh(signup)
    assert signup.status == SignupStatus.cancelled
    assert len(calls) == 1
    assert calls[0][0] == "U0STU"
    assert "Food Drive" in calls[0][1]

    # Removing an already-cancelled signup is a no-op — no duplicate DM.
    resp = await client.post(f"/admin/shifts/{shift.id}/signups/{signup.id}/remove")
    assert len(calls) == 1


async def test_admin_remove_signup_skips_dm_without_slack_id(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []
    monkeypatch.setattr(adminmod, "send_dm", lambda *a, **k: calls.append(a))

    await _login(client)
    student = await make_student(name="No Slack", slack=None)
    opp = await make_opportunity()
    shift = await make_shift(opp.id)
    signup = Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up)
    db.add(signup)
    await db.commit()
    await db.refresh(signup)

    await client.post(f"/admin/shifts/{shift.id}/signups/{signup.id}/remove")
    await db.refresh(signup)
    assert signup.status == SignupStatus.cancelled
    assert calls == []  # no Slack ID -> no DM attempted


async def test_roster_sync_now_button(client, monkeypatch):
    import app.routers.admin as adminmod

    async def fake_sync(db):
        return "1 students, 1 mentors"

    monkeypatch.setattr("app.services.legion_sync.sync_roster", fake_sync)
    await _login(client)
    resp = await client.post("/admin/roster/sync", follow_redirects=False)
    assert resp.status_code == 303
    assert "synced=" in resp.headers["location"]


async def test_admin_my_dashboard_link_shown_for_student_role(client):
    await _login(client, role="student")
    page = await client.get("/admin/opportunities")
    assert "My Dashboard" in page.text


async def test_admin_my_dashboard_link_hidden_for_mentor_role(client):
    await _login(client, role="mentor")
    page = await client.get("/admin/opportunities")
    assert "My Dashboard" not in page.text
