"""End-to-end smoke tests for the student portal (exercises the Jinja templates)."""


async def _identify(client, code: str):
    return await client.post("/identify", data={"student_code": code})


async def test_identify_and_browse(client, make_student, make_mentor, make_opportunity, make_shift):
    student = await make_student(code="ada00001")
    await make_mentor(name="Coach Ray")
    opp = await make_opportunity(name="Food Drive", location="Community Center")
    shift = await make_shift(opp.id, capacity=2)

    # Bad code is rejected.
    bad = await _identify(client, "nope")
    assert bad.status_code == 401

    resp = await _identify(client, "ada00001")
    assert resp.status_code == 303

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


async def test_portal_requires_identity(client):
    resp = await client.get("/opportunities")
    assert resp.status_code == 303  # redirected to landing


async def test_magic_link_signs_in_and_lands_on_dashboard(client, make_student):
    from app.services.student_auth import make_magic_token

    student = await make_student(code="zzz00001")
    resp = await client.get(f"/enter?token={make_magic_token(student.id)}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    home = await client.get("/")  # cookie now set -> dashboard renders
    assert home.status_code == 200
    assert "Season total" in home.text


async def test_magic_link_deep_links_with_next(client, make_student):
    from app.services.student_auth import make_magic_token

    student = await make_student(code="zzz00002")
    resp = await client.get(
        f"/enter?token={make_magic_token(student.id)}&next=%2Fsubmit", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/submit"


async def test_invalid_magic_link_shows_hint(client):
    resp = await client.get("/enter?token=bogus")
    assert resp.status_code == 401
    assert "expired" in resp.text.lower()
    assert "/vhours" in resp.text


async def test_dashboard_shows_projected_hours(client, db, make_student, make_opportunity, make_shift):
    from app.models import Signup, SignupStatus

    student = await make_student(code="proj0001")
    opp = await make_opportunity(name="Build Day")
    shift = await make_shift(opp.id, start_in_hours=24, length_hours=4)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    await _identify(client, "proj0001")
    home = await client.get("/")
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
    client, db, make_student, make_mentor, make_opportunity, make_shift
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
