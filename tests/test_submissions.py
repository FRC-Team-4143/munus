from app.models import SubmissionStatus
from app.services.requirements import season_total_hours
from app.services.submissions import create_submission, set_status


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
