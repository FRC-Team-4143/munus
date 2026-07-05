from sqlalchemy import select

import app.services.scheduler as scheduler
from app.models import Signup, SignupStatus


async def test_post_shift_prompt_sends_interactive_dm(
    db, session_factory, make_student, make_opportunity, make_shift, monkeypatch
):
    student = await make_student(slack="U0STU")
    opp = await make_opportunity()
    ended = await make_shift(opp.id, start_in_hours=-3, length_hours=2)  # already ended
    db.add(Signup(shift_id=ended.id, student_id=student.id, status=SignupStatus.signed_up))
    await db.commit()

    calls = []

    async def fake_send_dm(uid, text, blocks=None):
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
