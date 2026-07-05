from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Opportunity, Shift, Signup, SignupStatus, SubmissionStatus
from app.services.requirements import season_total_hours
from app.services.submissions import (
    create_submission, resolve_reviewer_id, set_status, submit_shift_hours,
)
from app.utils import shift_length_hours


async def test_create_submission_is_pending(db, make_student, make_mentor, make_opportunity):
    student = await make_student()
    mentor = await make_mentor()
    opp = await make_opportunity()
    sub = await create_submission(
        db, student_id=student.id, opportunity_id=opp.id, shift_id=None,
        hours=4.0, report="Sorted cans", reviewer_mentor_id=mentor.id,
    )
    assert sub.status == SubmissionStatus.pending
    assert sub.reviewer_mentor_id == mentor.id
    # Pending hours do not count yet.
    assert await season_total_hours(db, student.id) == 0.0


async def test_create_submission_approved_backdated(db, make_student):
    from datetime import datetime

    student = await make_student()
    when = datetime(2026, 1, 15, 12, 0)
    sub = await create_submission(
        db, student_id=student.id, opportunity_id=None, shift_id=None,
        hours=6.0, report="Preseason", reviewer_mentor_id=None,
        status=SubmissionStatus.approved, submitted_at=when,
    )
    assert sub.status == SubmissionStatus.approved
    assert sub.submitted_at == when          # honours the back-dated date
    assert sub.reviewed_at is not None       # a decided status stamps reviewed_at
    # Approved hours count immediately toward the season total.
    assert await season_total_hours(db, student.id) == 6.0


async def test_approve_counts_hours(db, make_student, make_mentor):
    student = await make_student()
    mentor = await make_mentor()
    sub = await create_submission(
        db, student_id=student.id, opportunity_id=None, shift_id=None,
        hours=3.5, report=None, reviewer_mentor_id=mentor.id,
    )
    updated = await set_status(db, sub.id, SubmissionStatus.approved)
    assert updated.status == SubmissionStatus.approved
    assert updated.reviewed_at is not None
    assert await season_total_hours(db, student.id) == 3.5


async def test_reject_does_not_count(db, make_student, make_mentor):
    student = await make_student()
    mentor = await make_mentor()
    sub = await create_submission(
        db, student_id=student.id, opportunity_id=None, shift_id=None,
        hours=3.5, report=None, reviewer_mentor_id=mentor.id,
    )
    updated = await set_status(db, sub.id, SubmissionStatus.rejected, review_note="Not eligible")
    assert updated.status == SubmissionStatus.rejected
    assert updated.review_note == "Not eligible"
    assert await season_total_hours(db, student.id) == 0.0


async def test_set_status_missing_returns_none(db):
    assert await set_status(db, 9999, SubmissionStatus.approved) is None


def test_resolve_reviewer_prefers_shift_then_opportunity():
    opp = Opportunity(name="X", reviewer_mentor_id=5)
    shift = Shift(reviewer_mentor_id=None)
    shift.opportunity = opp
    # No shift override -> opportunity default.
    assert resolve_reviewer_id(shift) == 5
    # Shift override wins.
    shift.reviewer_mentor_id = 9
    assert resolve_reviewer_id(shift) == 9
    # Neither set -> None (admin queue).
    shift.reviewer_mentor_id = None
    opp.reviewer_mentor_id = None
    assert resolve_reviewer_id(shift) is None


async def _signup_with_shift(db, make_student, make_opportunity, make_shift,
                             opp_reviewer=None, shift_reviewer=None):
    opp = await make_opportunity(reviewer_mentor_id=opp_reviewer)
    shift = await make_shift(opp.id, length_hours=3)
    if shift_reviewer is not None:
        shift.reviewer_mentor_id = shift_reviewer
        await db.commit()
    student = await make_student()
    db.add(Signup(shift_id=shift.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()
    signup = (
        await db.execute(
            select(Signup)
            .options(selectinload(Signup.shift).selectinload(Shift.opportunity))
            .where(Signup.shift_id == shift.id, Signup.student_id == student.id)
        )
    ).scalars().first()
    return signup, shift, student


async def test_submit_shift_hours_routes_to_resolved_reviewer(
    db, make_student, make_mentor, make_opportunity, make_shift
):
    default_mentor = await make_mentor(name="Opp Default", slack="U0DEF")
    signup, shift, student = await _signup_with_shift(
        db, make_student, make_opportunity, make_shift, opp_reviewer=default_mentor.id
    )
    default_hours = shift_length_hours(shift.start_time, shift.end_time)

    sub = await submit_shift_hours(db, signup, default_hours, "Sorted cans")
    assert sub is not None
    assert sub.status == SubmissionStatus.pending
    assert sub.hours == 3.0
    assert sub.shift_id == shift.id
    assert sub.reviewer_mentor_id == default_mentor.id  # opportunity default

    # Idempotent — a second tap does not create a duplicate.
    assert await submit_shift_hours(db, signup, default_hours, None) is None


async def test_submit_shift_hours_shift_override_wins(
    db, make_student, make_mentor, make_opportunity, make_shift
):
    default_mentor = await make_mentor(name="Default", slack="U0DEF")
    override_mentor = await make_mentor(name="Override", slack="U0OVR")
    signup, shift, student = await _signup_with_shift(
        db, make_student, make_opportunity, make_shift,
        opp_reviewer=default_mentor.id, shift_reviewer=override_mentor.id,
    )
    sub = await submit_shift_hours(db, signup, 2.0, None)
    assert sub.reviewer_mentor_id == override_mentor.id
