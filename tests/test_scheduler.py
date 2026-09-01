from datetime import datetime, timedelta

from sqlalchemy import select

import app.services.scheduler as scheduler
from app.models import HourSubmission, Opportunity, Signup, SignupStatus, SubmissionStatus


async def test_post_shift_prompt_sends_interactive_dm(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    ended = await make_shift(opp.id, start_in_hours=-3, length_hours=2)  # already ended
    db.add(Signup(shift_id=ended.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    calls = []

    async def fake_send_dm(uid, text, blocks=None, automated=False):
        calls.append((uid, text, blocks))
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    await scheduler.job_post_shift_prompts()

    assert len(calls) == 1
    uid, _text, blocks = calls[0]
    assert uid == "U0STU"
    # Interactive blocks with the one-tap "Log" button, not a plain link.
    section = next(b for b in blocks if b["type"] == "section")
    assert "Log your hours" in section["text"]["text"]
    actions = next(b for b in blocks if b["type"] == "actions")
    assert {e["action_id"] for e in actions["elements"]} == {"hours_quick", "hours_adjust"}

    # Prompted once → won't be re-sent.
    su = (await db.execute(select(Signup).where(Signup.student_id == student.id))).scalars().first()
    assert su.prompted_at is not None


async def test_post_shift_prompt_skips_archived_student(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    """An archived student's outstanding signup must not generate a post-shift DM —
    they're no longer supposed to be visible/actionable anywhere in Munus."""
    student = await make_student(slack="U0GRAD", is_active=False)
    opp = await make_opportunity()
    ended = await make_shift(opp.id, start_in_hours=-3, length_hours=2)
    db.add(Signup(shift_id=ended.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_post_shift_prompts()
    assert calls == []


async def test_post_shift_prompt_skips_already_submitted(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    from app.models import HourSubmission, SubmissionStatus

    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    ended = await make_shift(opp.id, start_in_hours=-3, length_hours=2)
    db.add(Signup(shift_id=ended.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(HourSubmission(student_id=student.id, shift_id=ended.id, hours=2.0,
                          status=SubmissionStatus.pending))
    await db.commit()

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_post_shift_prompts()
    assert calls == []  # already logged → no prompt


async def test_auto_reject_closes_unlogged_shift(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    from app.models import HourSubmission, SubmissionStatus

    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    old = await make_shift(opp.id, start_in_hours=-200, length_hours=2)     # ended ~8 days ago
    recent = await make_shift(opp.id, start_in_hours=-24, length_hours=2)   # ended ~1 day ago
    db.add(Signup(shift_id=old.id, student_id=student.id, status=SignupStatus.signed_up))
    db.add(Signup(shift_id=recent.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    await scheduler.job_auto_reject_unlogged()

    subs = (await db.execute(select(HourSubmission))).scalars().all()
    # Only the shift past the 7-day window is closed out; the recent one is left alone.
    assert len(subs) == 1
    assert subs[0].shift_id == old.id
    assert subs[0].status == SubmissionStatus.rejected
    assert "not submitted" in subs[0].review_note
    assert len(calls) == 1 and calls[0][0] == "U0STU"

    # Idempotent: a second run finds the existing (rejected) submission and adds nothing.
    await scheduler.job_auto_reject_unlogged()
    assert len((await db.execute(select(HourSubmission))).scalars().all()) == 1


async def test_auto_reject_skips_archived_student(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    """An archived student's unlogged shift must not get an auto-reject submission
    recorded against them."""
    from app.models import HourSubmission

    student = await make_student(slack="U0GRAD", is_active=False)
    opp = await make_opportunity()
    old = await make_shift(opp.id, start_in_hours=-200, length_hours=2)
    db.add(Signup(shift_id=old.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    async def fake_send_dm(*a, **k):
        pass

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    await scheduler.job_auto_reject_unlogged()

    assert (await db.execute(select(HourSubmission))).scalars().all() == []


async def test_auto_reject_skips_submitted_and_respects_disable(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    from app.models import HourSubmission, SubmissionStatus

    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    old = await make_shift(opp.id, start_in_hours=-200, length_hours=2)
    db.add(Signup(shift_id=old.id, student_id=student.id, status=SignupStatus.signed_up))
    # Already logged (pending) → must not be auto-rejected.
    db.add(HourSubmission(student_id=student.id, shift_id=old.id, hours=2.0,
                          status=SubmissionStatus.pending))
    await db.commit()

    async def fake_send_dm(*a, **k):
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    await scheduler.job_auto_reject_unlogged()
    subs = (await db.execute(select(HourSubmission))).scalars().all()
    assert len(subs) == 1 and subs[0].status == SubmissionStatus.pending  # untouched

    # Disabled (0 days) → no-op even for an eligible unlogged shift.
    other = await make_student(code="dis00001", slack="U0DIS")
    old2 = await make_shift(opp.id, start_in_hours=-300, length_hours=2)
    db.add(Signup(shift_id=old2.id, student_id=other.id, status=SignupStatus.signed_up))
    await db.commit()
    monkeypatch.setattr(scheduler.settings, "auto_reject_days", 0)
    await scheduler.job_auto_reject_unlogged()
    other_subs = (
        await db.execute(select(HourSubmission).where(HourSubmission.student_id == other.id))
    ).scalars().all()
    assert other_subs == []


async def _pending_submission(
    db, student, mentor, opp, *, days_old, reminder_days_ago=None,
    status=SubmissionStatus.pending,
):
    """A submission dated `days_old` days ago, optionally already nudged
    `reminder_days_ago` days ago. `mentor=None` leaves it unrouted."""
    now = datetime.utcnow()
    sub = HourSubmission(
        student_id=student.id,
        opportunity_id=opp.id,
        shift_id=None,
        hours=2.0,
        report="Set up tables",
        reviewer_mentor_id=mentor.id if mentor else None,
        status=status,
        submitted_at=now - timedelta(days=days_old),
        reminder_sent_at=(
            now - timedelta(days=reminder_days_ago) if reminder_days_ago is not None else None
        ),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def test_pending_reminder_dms_reviewer(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    sub = await _pending_submission(db, student, mentor, opp, days_old=4)

    calls = []

    async def fake_send_dm(uid, text, blocks=None, automated=False):
        calls.append((uid, text, blocks))
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    await scheduler.job_pending_review_reminders()

    assert len(calls) == 1
    uid, _text, blocks = calls[0]
    assert uid == "U0REV"
    # The nudge carries the standard, actionable review card.
    actions = next(b for b in blocks if b["type"] == "actions")
    assert {e["action_id"] for e in actions["elements"]} == {
        "submission_approve", "review_edit", "submission_reject"
    }

    await db.refresh(sub)
    assert sub.reminder_sent_at is not None

    # Debounced: an immediate second run finds a fresh reminder_sent_at and does nothing.
    await scheduler.job_pending_review_reminders()
    assert len(calls) == 1


async def test_pending_reminder_skips_recent_submission(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    await _pending_submission(db, student, mentor, opp, days_old=1)  # inside the 3-day window

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_pending_review_reminders()
    assert calls == []


async def test_pending_reminder_recurs_after_another_window(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    # Nudged once already — 1 day ago is still inside the window, 4 days ago is past it.
    fresh = await _pending_submission(db, student, mentor, opp, days_old=10, reminder_days_ago=1)
    stale = await _pending_submission(db, student, mentor, opp, days_old=20, reminder_days_ago=4)

    calls = []

    async def fake_send_dm(uid, text, blocks=None, automated=False):
        calls.append((uid, text))
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_pending_review_reminders()

    assert len(calls) == 1
    await db.refresh(fresh)
    await db.refresh(stale)
    assert fresh.reminder_sent_at < stale.reminder_sent_at  # only `stale` was re-stamped


async def test_pending_reminder_ignores_decided_submissions(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    await _pending_submission(db, student, mentor, opp, days_old=10, status=SubmissionStatus.approved)
    await _pending_submission(db, student, mentor, opp, days_old=10, status=SubmissionStatus.rejected)

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_pending_review_reminders()
    assert calls == []


async def test_pending_reminder_skips_when_no_reachable_reviewer(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    archived = await make_mentor(name="Gone", slack="U0GONE", is_active=False)
    no_slack = await make_mentor(name="No Slack", slack=None)
    subs = [
        await _pending_submission(db, student, archived, opp, days_old=5),
        await _pending_submission(db, student, no_slack, opp, days_old=5),
        await _pending_submission(db, student, None, opp, days_old=5),  # unrouted → Admin queue
    ]

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_pending_review_reminders()

    assert calls == []
    for s in subs:
        await db.refresh(s)
        assert s.reminder_sent_at is None  # not stamped → a later reviewer still gets nudged


async def test_pending_reminder_skips_archived_student(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU", is_active=False)
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    await _pending_submission(db, student, mentor, opp, days_old=5)

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)
    await scheduler.job_pending_review_reminders()
    assert calls == []


async def test_pending_reminder_respects_disable(
    db, session_factory, make_student, make_mentor, make_opportunity, monkeypatch
):
    student = await make_student(slack="U0STU")
    mentor = await make_mentor(slack="U0REV")
    opp = await make_opportunity()
    await _pending_submission(db, student, mentor, opp, days_old=5)

    calls = []

    async def fake_send_dm(*a, **k):
        calls.append(a)
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    monkeypatch.setattr(scheduler.settings, "pending_reminder_days", 0)
    await scheduler.job_pending_review_reminders()
    assert calls == []

    monkeypatch.setattr(scheduler.settings, "pending_reminder_days", 3)
    monkeypatch.setattr(scheduler.settings, "updates_enabled", False)
    await scheduler.job_pending_review_reminders()
    assert calls == []


async def test_auto_archive_opportunity_after_last_shift(
    db, session_factory, make_opportunity, make_shift, monkeypatch
):
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    stale = await make_opportunity(name="Past Bake Sale")
    await make_shift(stale.id, start_in_hours=-72, length_hours=2)  # ended ~2 days ago

    recent = await make_opportunity(name="Recent Cleanup")
    await make_shift(recent.id, start_in_hours=-6, length_hours=2)  # ended a few hours ago

    ongoing = await make_opportunity(name="CAD Subteam", is_continuous=True)

    await scheduler.job_auto_archive_opportunities()

    await db.refresh(stale)
    await db.refresh(recent)
    await db.refresh(ongoing)
    assert stale.is_active is False
    assert stale.archived_at is not None
    assert recent.is_active is True  # inside the 1-day grace window
    assert ongoing.is_active is True  # continuous opps are never auto-archived

    # Idempotent: a second run doesn't touch the already-archived one again.
    prev_archived_at = stale.archived_at
    await scheduler.job_auto_archive_opportunities()
    await db.refresh(stale)
    assert stale.archived_at == prev_archived_at


async def test_auto_archive_respects_disable_and_hours_persist(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)

    student = await make_student(code="ada00001")
    opp = await make_opportunity(name="Past Bake Sale")
    shift = await make_shift(opp.id, start_in_hours=-72, length_hours=2)
    db.add(HourSubmission(
        student_id=student.id, opportunity_id=opp.id, shift_id=shift.id,
        hours=2.0, status=SubmissionStatus.approved,
    ))
    await db.commit()

    monkeypatch.setattr(scheduler.settings, "auto_archive_days", 0)
    await scheduler.job_auto_archive_opportunities()
    await db.refresh(opp)
    assert opp.is_active is True  # disabled → no-op

    monkeypatch.setattr(scheduler.settings, "auto_archive_days", 1)
    await scheduler.job_auto_archive_opportunities()
    await db.refresh(opp)
    assert opp.is_active is False

    # Archiving never deletes the logged hours.
    subs = (await db.execute(select(HourSubmission).where(HourSubmission.opportunity_id == opp.id))).scalars().all()
    assert len(subs) == 1
    assert subs[0].status == SubmissionStatus.approved
