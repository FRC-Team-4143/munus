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


@pytest.mark.parametrize("path", [
    "/admin", "/admin/opportunities", "/admin/submissions", "/admin/roster",
    "/admin/report", "/admin/audit", "/admin/backup", "/admin/settings",
])
async def test_admin_pages_render(client, path):
    await _login(client)
    resp = await client.get(path)
    assert resp.status_code == 200


async def test_send_prompt_button_dms_signed_up_students(
    client, db, monkeypatch, make_student, make_opportunity, make_shift
):
    import app.routers.admin as adminmod
    from app.models import Signup, SignupStatus

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, blocks))
        return "ts"

    monkeypatch.setattr(adminmod, "send_dm", fake_send_dm)

    await _login(client)
    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    shift = await make_shift(opp.id)  # future shift — button ignores timing
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    resp = await client.post(f"/admin/shifts/{shift.id}/send-prompt", follow_redirects=False)
    assert resp.status_code == 303
    assert "prompt_sent=1" in resp.headers["location"]
    assert len(calls) == 1 and calls[0][0] == "U0STU"
    assert any(b.get("type") == "actions" for b in calls[0][1])  # interactive prompt


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


async def test_manager_role_scoped_to_opportunities(client):
    await _login(client, groups=("munus-manager",))

    # Can view + create opportunities.
    assert (await client.get("/admin/opportunities")).status_code == 200
    cr = await client.post("/admin/opportunities", data={"name": "Mgr Opp"}, follow_redirects=False)
    assert cr.status_code == 303 and "/admin/opportunities/" in cr.headers["location"]

    # Blocked from every admin-only section — stays in the admin shell with a
    # blur-blocked "No Access" page rather than being silently redirected away
    # (regression test: it used to 303 to Opportunities with no explanation).
    for path in ("/admin", "/admin/roster", "/admin/submissions", "/admin/settings", "/admin/backup", "/admin/report"):
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


async def test_admin_report_export_csv(client):
    await _login(client)
    resp = await client.get("/admin/report/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "Student,Level,Approved Hours,Projected Hours" in resp.text


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
        "start_time": "2026-08-01T09:00", "end_time": "2026-08-01T12:00",
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
        data={"start_time": "2026-08-01T15:00", "end_time": "2026-08-01T14:00", "capacity": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    count = (await db.execute(select(func.count()).select_from(Shift))).scalar_one()
    assert count == 0


async def test_admin_edit_shift_updates_fields(client, db, make_opportunity, make_shift):
    from datetime import datetime
    from app.utils import local_to_utc

    await _login(client)
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=2)

    resp = await client.post(
        f"/admin/shifts/{shift.id}/edit",
        data={
            "start_time": "2026-09-01T09:00", "end_time": "2026-09-01T13:00",
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
        data={"start_time": "2026-08-01T15:00", "end_time": "2026-08-01T14:00", "capacity": "0"},
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
        data={"start_time": "2026-09-01T09:00", "end_time": "2026-09-01T13:00", "capacity": "0"},
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
