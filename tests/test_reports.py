from datetime import datetime, timedelta

from app.models import HourSubmission, Signup, SignupStatus, StudentLevel, SubmissionStatus
from app.services.app_settings import set_season_start
from app.services.reports import student_progress_report


async def test_report_sticky_projected(db, make_student, make_opportunity, make_shift):
    """Projected stays stable across a shift's lifecycle: approved counts at its real value,
    a pending submission counts at its submitted value, an ended shift not yet logged still
    counts at its scheduled length, and a rejected submission drops out entirely."""
    student = await make_student(name="Ada", code="ada00001", level=StudentLevel.freshman)  # req 5
    opp = await make_opportunity()

    # 2h approved (ad-hoc, no shift link).
    db.add(HourSubmission(student_id=student.id, hours=2.0, status=SubmissionStatus.approved))

    # Upcoming 3h signed-up shift, not yet logged -> scheduled estimate.
    upcoming = await make_shift(opp.id, start_in_hours=24, length_hours=3)
    db.add(Signup(shift_id=upcoming.id, student_id=student.id, status=SignupStatus.signed_up))

    # Ended 1h signed-up shift, not yet logged -> still counts (this used to dip to zero).
    ended_unlogged = await make_shift(opp.id, start_in_hours=-5, length_hours=1)
    db.add(Signup(shift_id=ended_unlogged.id, student_id=student.id, status=SignupStatus.signed_up))

    # Ended 4h signed-up shift with a *pending* submission -> counts at the pending value (4),
    # not the scheduled length; the shift is excluded from the scheduled estimate.
    ended_pending = await make_shift(opp.id, start_in_hours=-6, length_hours=4)
    db.add(Signup(shift_id=ended_pending.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(
        student_id=student.id, shift_id=ended_pending.id, hours=4.0,
        status=SubmissionStatus.pending,
    ))

    # Ended signed-up shift whose submission was rejected -> contributes nothing.
    ended_rejected = await make_shift(opp.id, start_in_hours=-8, length_hours=2)
    db.add(Signup(shift_id=ended_rejected.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(
        student_id=student.id, shift_id=ended_rejected.id, hours=5.0,
        status=SubmissionStatus.rejected,
    ))
    await db.commit()

    rows = await student_progress_report(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["approved"] == 2.0
    # 2 approved + 4 pending + (3 upcoming + 1 ended-unlogged) scheduled = 10.0.
    assert r["projected"] == 10.0
    assert r["required"] == 5.0
    assert r["remaining"] == 3.0  # remaining is vs approved only
    assert r["met"] is False
    assert r["pending_count"] == 1
    assert r["upcoming_count"] == 1  # only the shift that hasn't ended
    assert r["missing_required"] == []  # no required opportunities configured


async def test_report_level_filter_excludes_archived(db, make_student):
    await make_student(name="Fresh", code="frsh0001", level=StudentLevel.freshman)
    await make_student(name="Senior", code="snr00001", level=StudentLevel.team_4143)
    await make_student(name="Gone", code="gone0001", level=StudentLevel.freshman, is_active=False)

    all_rows = await student_progress_report(db)
    assert {r["student"].name for r in all_rows} == {"Fresh", "Senior"}  # archived excluded
    assert all(r["missing_required"] == [] for r in all_rows)

    fresh_only = await student_progress_report(db, level=StudentLevel.freshman)
    assert {r["student"].name for r in fresh_only} == {"Fresh"}


async def test_student_vhours_message(db, make_student, make_opportunity, make_shift):
    from app.models import Signup, SignupStatus
    from app.services.reports import student_vhours_message

    student = await make_student(name="Ada", code="vh000001", level=StudentLevel.freshman)
    opp = await make_opportunity(name="Park Cleanup")
    shift = await make_shift(opp.id, start_in_hours=48, length_hours=3)  # upcoming
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    msg = await student_vhours_message(db, student)
    assert "Your Volunteer Hours" in msg
    assert "Season total:" in msg
    assert "Park Cleanup" in msg        # the upcoming shift is listed
    assert f"/enter?member={student.member_code}" in msg  # one-tap dashboard link


async def test_student_vhours_message_lists_available_opportunities_when_short(
    db, make_student, make_opportunity, make_shift
):
    """Still short of the season requirement even after upcoming shifts -> nudge with a
    few opportunities the student hasn't signed up for yet, each linking through /enter."""
    from app.models import Signup, SignupStatus
    from app.services.reports import student_vhours_message

    student = await make_student(name="Ada", code="vh000002", level=StudentLevel.team_4143)  # req 15
    signed_up_opp = await make_opportunity(name="Already Signed Up")
    signed_up_shift = await make_shift(signed_up_opp.id, start_in_hours=24, length_hours=1)
    db.add(Signup(shift_id=signed_up_shift.id, student_id=student.id, status=SignupStatus.signed_up))

    open_opp = await make_opportunity(name="Robotics Demo")
    await make_shift(open_opp.id, start_in_hours=48, length_hours=2)

    continuous_opp = await make_opportunity(name="Shop Cleanup", is_continuous=True)
    await db.commit()

    msg = await student_vhours_message(db, student)
    assert "Opportunities you could sign up for:" in msg
    available_section = msg.split("Opportunities you could sign up for:")[1]
    assert "Robotics Demo" in available_section
    assert "Shop Cleanup" in available_section
    assert "Already Signed Up" not in available_section  # already signed up -> not renudged
    assert f"/enter?member={student.member_code}&next=/opportunities/{open_opp.id}" in msg


async def test_student_vhours_message_omits_available_opportunities_when_met(
    db, make_student
):
    from app.services.reports import student_vhours_message

    student = await make_student(name="Ada", code="vh000003", level=StudentLevel.freshman)  # req 5
    db.add(HourSubmission(student_id=student.id, hours=6.0, status=SubmissionStatus.approved))
    await db.commit()

    msg = await student_vhours_message(db, student)
    assert "Opportunities you could sign up for:" not in msg


async def test_report_met_when_requirement_reached(db, make_student):
    student = await make_student(level=StudentLevel.freshman)  # required 5
    db.add(HourSubmission(student_id=student.id, hours=6.0, status=SubmissionStatus.approved))
    await db.commit()
    r = (await student_progress_report(db))[0]
    assert r["met"] is True
    assert r["remaining"] == 0.0
    assert r["pct"] == 100
    assert r["missing_required"] == []


async def test_report_missing_required_opportunity(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(opp.id, start_in_hours=24)

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Bag Night"]


async def test_report_fulfilled_required_opportunity_via_signup(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    shift = await make_shift(opp.id, start_in_hours=24)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == []


async def test_report_cancelled_signup_does_not_fulfill_required(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    shift = await make_shift(opp.id, start_in_hours=24)
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.cancelled))
    await db.commit()

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Bag Night"]


async def test_report_stale_required_opportunity_not_flagged(db, make_student, make_opportunity, make_shift):
    """The headline new-student-protection scenario: a required opportunity whose only
    shift predates the season-start cutoff doesn't apply to anyone -- old or new. A
    second, live required opportunity is still correctly flagged."""
    student = await make_student(level=StudentLevel.freshman)
    old_opp = await make_opportunity(name="Old Fundraiser", is_required=True)
    await make_shift(old_opp.id, start_in_hours=-24 * 40)
    live_opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(live_opp.id, start_in_hours=24)

    await set_season_start(db, (datetime.utcnow() - timedelta(days=7)).date())

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Bag Night"]


async def test_report_stale_signup_does_not_fulfill_live_requirement(db, make_student, make_opportunity, make_shift):
    """Fulfillment is scoped to the same season cutoff as the requirement itself -- a
    signup for a pre-cutoff shift of an otherwise-live required opportunity doesn't
    count, only a signup for a qualifying (post-cutoff) shift does."""
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    old_shift = await make_shift(opp.id, start_in_hours=-24 * 40)
    await make_shift(opp.id, start_in_hours=24)  # keeps the opportunity "live"
    db.add(Signup(shift_id=old_shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    await set_season_start(db, (datetime.utcnow() - timedelta(days=7)).date())

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Bag Night"]


async def test_report_ignores_continuous_required_inconsistency(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="CAD Subteam", is_required=True, is_continuous=True)
    await make_shift(opp.id)

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == []


async def test_report_archived_required_opportunity_still_flags_student(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    opp = await make_opportunity(name="Bag Night", is_required=True)
    await make_shift(opp.id, start_in_hours=-5, length_hours=1)
    opp.is_active = False
    opp.archived_at = datetime.utcnow()
    await db.commit()

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Bag Night"]


async def test_report_partial_fulfillment_across_multiple_required(db, make_student, make_opportunity, make_shift):
    student = await make_student(level=StudentLevel.freshman)
    fulfilled_opp = await make_opportunity(name="Bag Night", is_required=True)
    fulfilled_shift = await make_shift(fulfilled_opp.id, start_in_hours=24)
    db.add(Signup(shift_id=fulfilled_shift.id, student_id=student.id, status=SignupStatus.signed_up))
    missed_opp = await make_opportunity(name="Fundraiser", is_required=True)
    await make_shift(missed_opp.id, start_in_hours=48)
    await db.commit()

    r = (await student_progress_report(db))[0]
    assert r["missing_required"] == ["Fundraiser"]
