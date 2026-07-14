"""End-to-end smoke tests for the student portal (exercises the Jinja templates)."""
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


async def _identify(client, code: str):
    """Set a valid `mw_sso` cookie for the student with this `member_code` — the portal
    has no cookie/token of its own, it's the same Legion identity `/admin` uses."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(role="student", member_code=code, groups=()))


async def test_identify_and_browse(client, make_student, make_mentor, make_opportunity, make_shift):
    student = await make_student(code="ada00001")
    await make_mentor(name="Coach Ray")
    opp = await make_opportunity(name="Food Drive", location="Community Center")
    shift = await make_shift(opp.id, capacity=2)

    await _identify(client, "ada00001")

    listing = await client.get("/opportunities")
    assert listing.status_code == 200
    assert "Food Drive" in listing.text

    detail = await client.get(f"/opportunities/{opp.id}")
    assert detail.status_code == 200
    assert "Community Center" in detail.text
    assert "Sign up" in detail.text

    signup = await client.post(f"/shifts/{shift.id}/signup")
    assert signup.status_code == 303

    after = await client.get(f"/opportunities/{opp.id}")
    assert "Signed up" in after.text

    # The signed-up shift is still in the future, so nothing is outstanding yet.
    submit_form = await client.get("/submit")
    assert submit_form.status_code == 200
    assert "all caught up" in submit_form.text

    my_hours = await client.get("/my-hours")
    assert my_hours.status_code == 200
    assert "Season total" in my_hours.text


async def test_continuous_opportunity_shows_log_hours_form(client, make_student, make_opportunity):
    await make_student(code="ada00001")
    opp = await make_opportunity(name="CAD Subteam", is_continuous=True)

    await _identify(client, "ada00001")

    listing = await client.get("/opportunities")
    assert listing.status_code == 200
    assert "Ongoing Activities" in listing.text
    assert "CAD Subteam" in listing.text

    detail = await client.get(f"/opportunities/{opp.id}")
    assert detail.status_code == 200
    assert "Log Hours" in detail.text
    assert "Sign up" not in detail.text


async def test_log_continuous_hours_creates_pending_submission(
    client, db, make_student, make_opportunity
):
    from sqlalchemy import select
    from app.models import HourSubmission, SubmissionStatus

    student = await make_student(code="ada00001")
    opp = await make_opportunity(name="CAD Subteam", is_continuous=True)
    await _identify(client, "ada00001")

    resp = await client.post(
        f"/opportunities/{opp.id}/log-hours",
        data={"hours": "2.5", "report": "Designed a bracket"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/opportunities/{opp.id}" in resp.headers["location"]

    sub = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == student.id))
    ).scalars().first()
    assert sub is not None
    assert sub.status == SubmissionStatus.pending
    assert sub.shift_id is None
    assert sub.opportunity_id == opp.id
    assert sub.hours == 2.5

    # Logging again is allowed — no idempotency guard for a continuous activity.
    resp2 = await client.post(
        f"/opportunities/{opp.id}/log-hours", data={"hours": "1.0"}, follow_redirects=False
    )
    assert resp2.status_code == 303
    count = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == student.id))
    ).scalars().all()
    assert len(count) == 2


async def test_log_hours_rejects_shift_based_opportunity(
    client, db, make_student, make_opportunity
):
    from sqlalchemy import select
    from app.models import HourSubmission

    await make_student(code="ada00001")
    opp = await make_opportunity(name="Food Drive")  # is_continuous defaults False
    await _identify(client, "ada00001")

    resp = await client.post(
        f"/opportunities/{opp.id}/log-hours", data={"hours": "2.0"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert (await db.execute(select(HourSubmission))).scalars().first() is None


async def test_log_hours_rejects_non_positive_hours(client, make_student, make_opportunity):
    await make_student(code="ada00001")
    opp = await make_opportunity(name="CAD Subteam", is_continuous=True)
    await _identify(client, "ada00001")

    resp = await client.post(
        f"/opportunities/{opp.id}/log-hours", data={"hours": "0"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "message=" in resp.headers["location"]


async def test_portal_requires_identity(client):
    resp = await client.get("/opportunities")
    assert resp.status_code == 303  # redirected to landing


async def test_unmatched_member_code_shows_identify_page(client):
    """An SSO identity that doesn't match any local Student row (e.g. not yet synced)
    still shows the sign-in page (with the Sign in button, since resolve_return_to
    needs it), but now says why instead of looking like sign-in silently failed."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(role="student", member_code="nope", groups=()))
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "Sign in with Legion" in resp.text
    assert "don't have an active student record" in resp.text


async def test_mentor_identity_cannot_reach_portal(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(role="mentor", groups=()))
    resp = await client.get("/opportunities")
    assert resp.status_code == 303


async def test_mentor_visiting_portal_home_sees_wrong_role_message(client):
    """Regression test: a mentor clicking "Sign in with Legion" on the student portal
    used to just silently re-show the same sign-in page, indistinguishable from sign-in
    having failed outright. It now says who they're signed in as and points at /admin."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(role="mentor", name="Coach Ray", groups=()))
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "Coach Ray" in resp.text
    assert "students only" in resp.text
    assert 'href="/admin"' in resp.text


async def test_signed_out_visitor_sees_plain_signin_prompt(client):
    """No cookie at all — the baseline case must not show either error message."""
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "Sign in with Legion" in resp.text
    assert "students only" not in resp.text
    assert "active student record" not in resp.text


async def test_enter_already_signed_in_skips_legion_challenge(client, make_student, monkeypatch):
    from app.services import legion_auth

    async def boom(*a, **kw):
        raise AssertionError("should not start a Legion challenge when already signed in")

    monkeypatch.setattr(legion_auth, "start_challenge", boom)
    student = await make_student(code="zzz00001")
    await _identify(client, "zzz00001")

    resp = await client.get(f"/enter?member={student.member_code}&next=/submit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/submit"


async def test_enter_unknown_member_falls_back_to_legion_authorize(client):
    resp = await client.get("/enter?member=doesnotexist", follow_redirects=False)
    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_enter_no_member_falls_back_to_legion_authorize(client):
    resp = await client.get("/enter", follow_redirects=False)
    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_enter_known_member_redirects_to_pending_page(client, make_student, monkeypatch):
    from app.config import settings
    from app.services import legion_auth

    student = await make_student(code="zzz00002")

    async def fake_start_challenge(member_code, *, return_to="/"):
        assert member_code == student.member_code
        # Absolute — Legion's /sso/complete redirects to return_to as-is, and a bare
        # relative path would resolve against Legion's own host on this cookie-less path.
        assert return_to == f"{settings.base_url}/submit"
        return "http://legion.test/sso/pending/abc123"

    monkeypatch.setattr(legion_auth, "start_challenge", fake_start_challenge)

    resp = await client.get(
        f"/enter?member={student.member_code}&next=/submit", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://legion.test/sso/pending/abc123"


async def test_enter_shows_unavailable_page_when_legion_unreachable(client, make_student, monkeypatch):
    from app.services import legion_auth

    student = await make_student(code="zzz00003")

    async def fake_start_challenge(member_code, *, return_to="/"):
        return None

    monkeypatch.setattr(legion_auth, "start_challenge", fake_start_challenge)

    resp = await client.get(f"/enter?member={student.member_code}")
    assert resp.status_code == 503
    assert "unavailable" in resp.text.lower()


async def test_dashboard_shows_projected_hours(client, db, make_student, make_opportunity, make_shift):
    from app.models import Signup, SignupStatus

    student = await make_student(code="proj0001")
    opp = await make_opportunity(name="Build Day")
    shift = await make_shift(opp.id, start_in_hours=24, length_hours=4)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    await _identify(client, "proj0001")
    home = await client.get("/me")
    assert home.status_code == 200
    assert "Projected" in home.text  # projected caption/segment shown


async def test_season_progress_sticky_projected(db, make_student, make_opportunity, make_shift):
    """The dashboard's projected estimate counts approved + pending + not-yet-logged shifts
    (including ended ones), while `upcoming` lists only shifts that haven't ended."""
    from app.models import HourSubmission, Signup, SignupStatus, StudentLevel, SubmissionStatus
    from app.routers.portal import _season_progress

    student = await make_student(code="stky0001", level=StudentLevel.freshman)  # required 5
    opp = await make_opportunity()
    db.add(HourSubmission(student_id=student.id, hours=2.0, status=SubmissionStatus.approved))

    upcoming = await make_shift(opp.id, start_in_hours=24, length_hours=3)
    db.add(Signup(shift_id=upcoming.id, student_id=student.id, status=SignupStatus.signed_up))

    ended_unlogged = await make_shift(opp.id, start_in_hours=-5, length_hours=1)
    db.add(Signup(shift_id=ended_unlogged.id, student_id=student.id, status=SignupStatus.signed_up))

    ended_pending = await make_shift(opp.id, start_in_hours=-6, length_hours=4)
    db.add(Signup(shift_id=ended_pending.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(
        student_id=student.id, shift_id=ended_pending.id, hours=4.0,
        status=SubmissionStatus.pending,
    ))
    await db.commit()

    p = await _season_progress(db, student)
    assert p["total"] == 2.0                # approved only (the solid bar)
    assert p["projected"] == 10.0           # 2 approved + 4 pending + (3 + 1) scheduled
    assert len(p["upcoming"]) == 1          # only the shift that hasn't ended is listed


async def test_in_progress_shift_still_visible(client, db, make_student, make_opportunity, make_shift):
    """A shift that has started but not ended should still show and be joinable."""
    await make_student(code="ada00001")
    opp = await make_opportunity(name="Cleanup")
    # Started an hour ago, ends in two hours.
    in_progress = await make_shift(opp.id, capacity=5, start_in_hours=-1, length_hours=3)

    await _identify(client, "ada00001")
    detail = await client.get(f"/opportunities/{opp.id}")
    assert detail.status_code == 200
    assert "Sign up" in detail.text  # the shift row rendered, not "No upcoming shifts"


# ── Submit hours (outstanding shifts) ────────────────────────────────────────

async def test_submit_lists_only_outstanding_shifts(
    client, db, make_student, make_opportunity, make_shift
):
    from sqlalchemy import select
    from app.models import HourSubmission, Signup, SignupStatus, SubmissionStatus

    student = await make_student(code="ada00001")
    opp = await make_opportunity(name="Food Drive")
    ended = await make_shift(opp.id, start_in_hours=-4, length_hours=2)      # ended, unlogged
    upcoming = await make_shift(opp.id, start_in_hours=24, length_hours=2)   # future
    logged = await make_shift(opp.id, start_in_hours=-50, length_hours=2)    # ended but logged

    for sh in (ended, upcoming, logged):
        db.add(Signup(shift_id=sh.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(student_id=student.id, opportunity_id=opp.id, shift_id=logged.id,
                          hours=2.0, status=SubmissionStatus.pending))
    await db.commit()

    async def signup_id(shift):
        return (await db.execute(
            select(Signup.id).where(Signup.shift_id == shift.id)
        )).scalar_one()

    await _identify(client, "ada00001")
    resp = await client.get("/submit")
    assert resp.status_code == 200
    body = resp.text
    assert "7 days" in body  # deadline banner (default auto_reject_days)
    assert f"/submit/{await signup_id(ended)}" in body           # offered
    assert f"/submit/{await signup_id(upcoming)}" not in body    # not ended yet
    assert f"/submit/{await signup_id(logged)}" not in body      # already logged


async def test_submit_shift_logs_hours_idempotently(
    client, db, hush_slack, make_student, make_mentor, make_opportunity, make_shift
):
    from sqlalchemy import func, select
    from app.models import HourSubmission, Signup, SignupStatus, SubmissionStatus

    mentor = await make_mentor(name="Coach Ray", slack=None)  # slack=None → notify no-ops
    student = await make_student(code="ada00001")
    opp = await make_opportunity(name="Food Drive", reviewer_mentor_id=mentor.id)
    shift = await make_shift(opp.id, start_in_hours=-4, length_hours=2)
    signup = Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up)
    db.add(signup)
    await db.commit()
    await db.refresh(signup)

    await _identify(client, "ada00001")
    resp = await client.post(
        f"/submit/{signup.id}", data={"hours": "1.5", "report": "Sorted cans"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sub = (await db.execute(
        select(HourSubmission).where(HourSubmission.shift_id == shift.id)
    )).scalars().one()
    assert sub.hours == 1.5
    assert sub.status == SubmissionStatus.pending
    assert sub.reviewer_mentor_id == mentor.id  # auto-resolved from the opportunity
    assert sub.report == "Sorted cans"

    # A second submit for the same shift is idempotent — no duplicate row.
    await client.post(f"/submit/{signup.id}", data={"hours": "3"}, follow_redirects=False)
    count = (await db.execute(
        select(func.count()).select_from(HourSubmission).where(HourSubmission.shift_id == shift.id)
    )).scalar_one()
    assert count == 1


async def test_submit_excludes_shift_that_has_not_started(
    client, db, make_student, make_opportunity, make_shift
):
    """A shift whose start is still in the future is never 'outstanding' — even one with a
    bad end-before-start time that would otherwise look already-ended."""
    from app.models import Signup, SignupStatus

    student = await make_student(code="ada00001")
    opp = await make_opportunity()
    # Starts in 1h, but end is (wrongly) 2h in the past — corrupt end-before-start shift.
    bad = await make_shift(opp.id, start_in_hours=1, length_hours=-3)
    db.add(Signup(shift_id=bad.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    await _identify(client, "ada00001")
    resp = await client.get("/submit")
    assert resp.status_code == 200
    assert "all caught up" in resp.text  # not offered for logging


async def test_submit_shift_rejects_non_positive_hours(
    client, db, make_student, make_opportunity, make_shift
):
    from sqlalchemy import func, select
    from app.models import HourSubmission, Signup, SignupStatus

    student = await make_student(code="ada00001")
    opp = await make_opportunity()
    shift = await make_shift(opp.id, start_in_hours=-4, length_hours=2)
    signup = Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up)
    db.add(signup)
    await db.commit()
    await db.refresh(signup)

    await _identify(client, "ada00001")
    resp = await client.post(f"/submit/{signup.id}", data={"hours": "0"}, follow_redirects=False)
    assert resp.status_code == 303
    count = (await db.execute(select(func.count()).select_from(HourSubmission))).scalar_one()
    assert count == 0


async def test_portal_admin_link_shown_for_manager_student(client, make_student):
    student = await make_student(code="mgr00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(
        role="student", member_code=student.member_code, groups=("munus-manager",),
    ))
    resp = await client.get("/me")
    assert '<a class="nav-link" href="/admin">' in resp.text


async def test_portal_admin_link_hidden_for_plain_student(client, make_student):
    student = await make_student(code="plain001")
    await _identify(client, "plain001")
    resp = await client.get("/me")
    assert '<a class="nav-link" href="/admin">' not in resp.text


async def test_portal_navbar_shows_legion_link_when_configured(client, make_student):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = "https://legion.example.org"
        await make_student(code="plain002")
        await _identify(client, "plain002")
        resp = await client.get("/me")
        assert 'href="https://legion.example.org"' in resp.text
    finally:
        settings.legion_base_url = original


async def test_portal_navbar_hides_legion_link_when_unconfigured(client, make_student):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = ""
        await make_student(code="plain003")
        await _identify(client, "plain003")
        resp = await client.get("/me")
        assert ">Legion</a>" not in resp.text
    finally:
        settings.legion_base_url = original


async def test_root_redirects_to_me(client):
    """The dashboard is canonically /me (matching Tempus); / just redirects to it."""
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/me"


async def test_dashboard_shows_admin_card_for_staff(client, make_student):
    student = await make_student(code="adm00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(
        role="student", member_code=student.member_code, groups=("munus-admin",),
    ))
    resp = await client.get("/me")
    assert "Open admin area" in resp.text


async def test_dashboard_hides_admin_card_for_plain_student(client, make_student):
    await make_student(code="plain002")
    await _identify(client, "plain002")
    resp = await client.get("/me")
    assert "Open admin area" not in resp.text
