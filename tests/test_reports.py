from app.models import HourSubmission, Signup, SignupStatus, StudentLevel, SubmissionStatus
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


async def test_report_level_filter_and_archived(db, make_student):
    await make_student(name="Fresh", code="frsh0001", level=StudentLevel.freshman)
    await make_student(name="Senior", code="snr00001", level=StudentLevel.team_4143)
    await make_student(name="Gone", code="gone0001", level=StudentLevel.freshman, is_active=False)

    all_rows = await student_progress_report(db)
    assert {r["student"].name for r in all_rows} == {"Fresh", "Senior"}  # archived excluded

    fresh_only = await student_progress_report(db, level=StudentLevel.freshman)
    assert {r["student"].name for r in fresh_only} == {"Fresh"}

    with_archived = await student_progress_report(db, include_archived=True)
    assert "Gone" in {r["student"].name for r in with_archived}


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


async def test_report_met_when_requirement_reached(db, make_student):
    student = await make_student(level=StudentLevel.freshman)  # required 5
    db.add(HourSubmission(student_id=student.id, hours=6.0, status=SubmissionStatus.approved))
    await db.commit()
    r = (await student_progress_report(db))[0]
    assert r["met"] is True
    assert r["remaining"] == 0.0
    assert r["pct"] == 100
