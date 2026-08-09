from app.config import settings
from app.models import Opportunity, SignupStatus
from app.services.opportunities import (
    active_signup_count, announce_opportunity, cancel_signup, get_signup,
    opportunity_announcement_blocks, remaining_capacity, signup_student, update_announcement,
)


def test_announcement_button_is_a_direct_link_not_an_action():
    """The button must be a plain Slack link button (a `url`, no `action_id`) so it
    never round-trips through our server — see opportunity_announcement_blocks's
    docstring for why this trades away per-clicker personalization."""
    original = settings.base_url
    try:
        settings.base_url = "https://munus.example.org"
        opp = Opportunity(id=42, name="Food Drive")
        text, blocks = opportunity_announcement_blocks(opp)

        assert "Food Drive" in text
        button = blocks[-1]["elements"][0]
        assert button["url"] == "https://munus.example.org/opportunities/42"
        assert "action_id" not in button
    finally:
        settings.base_url = original


def test_announcement_flags_required_opportunity():
    opp = Opportunity(id=1, name="Bag Night", is_required=True)
    text, blocks = opportunity_announcement_blocks(opp)
    assert "Required" in text
    assert blocks[0]["type"] == "header"  # title gets its own larger header block
    assert blocks[1]["text"]["text"] == "🚨 *Required — every active student must sign up for at least 1 shift.*"


def test_announcement_omits_required_flag_when_not_required():
    opp = Opportunity(id=1, name="Food Drive", is_required=False)
    text, _ = opportunity_announcement_blocks(opp)
    assert "Required" not in text


def test_announcement_header_truncates_long_names():
    """Slack's `header` block is plain_text and capped at 150 characters — a long
    opportunity name plus the "New volunteer opportunity:" prefix can exceed that."""
    opp = Opportunity(id=1, name="X" * 200)
    _, blocks = opportunity_announcement_blocks(opp)
    header_text = blocks[0]["text"]["text"]
    assert blocks[0]["type"] == "header"
    assert len(header_text) == 150
    assert header_text.endswith("…")


def test_announcement_includes_shift_date_range():
    from datetime import datetime
    from app.models import Shift

    opp = Opportunity(id=1, name="Craft Fair")
    opp.shifts = [
        Shift(start_time=datetime(2026, 11, 12, 18, 0), end_time=datetime(2026, 11, 12, 21, 0)),
        Shift(start_time=datetime(2026, 11, 14, 6, 0), end_time=datetime(2026, 11, 14, 11, 30)),
    ]
    text, _ = opportunity_announcement_blocks(opp)
    assert "📅" in text
    assert "Nov" in text  # exact day is timezone-dependent; the month isn't


def test_announcement_omits_date_line_when_no_shifts():
    """A continuous opportunity (or a shift-based one with no shifts yet) has nothing
    to show a date span for."""
    opp = Opportunity(id=1, name="CAD Subteam", is_continuous=True)
    text, _ = opportunity_announcement_blocks(opp)
    assert "📅" not in text


async def test_announce_opportunity_persists_channel_and_ts(db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module

    async def fake_post_to_channel(channel_id, text, blocks=None, automated=True):
        return "1699999999.000100"

    monkeypatch.setattr(opp_module, "post_to_channel", fake_post_to_channel)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = "C0ANNOUNCE"
    try:
        opp = await make_opportunity(name="Bag Night")
        # opportunity_announcement_blocks reads opp.shifts and can't lazy-load across
        # an async session (see its docstring) — callers always pass an eager-loaded
        # opp; mirror that here rather than relying on an implicit lazy load.
        await db.refresh(opp, attribute_names=["shifts"])
        ts = await announce_opportunity(db, opp)
        assert ts == "1699999999.000100"
        assert opp.announcement_channel_id == "C0ANNOUNCE"
        assert opp.announcement_ts == "1699999999.000100"
    finally:
        settings.slack_announce_channel = original


async def test_announce_opportunity_noop_without_channel_configured(db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module

    async def fail_if_called(*a, **k):
        raise AssertionError("post_to_channel should not be called")

    monkeypatch.setattr(opp_module, "post_to_channel", fail_if_called)
    original = settings.slack_announce_channel
    settings.slack_announce_channel = ""
    try:
        opp = await make_opportunity(name="Bag Night")
        assert await announce_opportunity(db, opp) is None
        assert opp.announcement_ts is None
    finally:
        settings.slack_announce_channel = original


async def test_update_announcement_noop_when_never_announced(db, make_opportunity, monkeypatch):
    """No channel/ts on record means this opportunity was never announced (blank
    SLACK_ANNOUNCE_CHANNEL, or announce_opportunity hasn't run yet) -- nothing to sync."""
    import app.services.opportunities as opp_module

    async def fail_if_called(*a, **k):
        raise AssertionError("update_channel_message should not be called")

    monkeypatch.setattr(opp_module, "update_channel_message", fail_if_called)
    opp = await make_opportunity(name="Bag Night")
    assert await update_announcement(db, opp) is None


async def test_update_announcement_pushes_current_details(db, make_opportunity, monkeypatch):
    import app.services.opportunities as opp_module

    calls = []

    async def fake_update_channel_message(channel_id, ts, text, blocks=None, automated=True):
        calls.append((channel_id, ts, text, blocks))
        return True

    monkeypatch.setattr(opp_module, "update_channel_message", fake_update_channel_message)
    opp = await make_opportunity(
        name="Bag Night", announcement_channel_id="C0ANNOUNCE", announcement_ts="1699999999.000100",
    )
    await db.refresh(opp, attribute_names=["shifts"])
    opp.name = "Bag Night (Rescheduled)"

    assert await update_announcement(db, opp) is True
    assert len(calls) == 1
    channel_id, ts, text, blocks = calls[0]
    assert channel_id == "C0ANNOUNCE"
    assert ts == "1699999999.000100"
    assert "Bag Night (Rescheduled)" in text


async def test_signup_and_capacity(db, make_student, make_mentor, make_opportunity, make_shift):
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=2)
    s1 = await make_student(name="A", code="aaaa1111")
    s2 = await make_student(name="B", code="bbbb2222")
    s3 = await make_student(name="C", code="cccc3333")

    ok, _ = await signup_student(db, shift, s1.id)
    assert ok
    ok, _ = await signup_student(db, shift, s2.id)
    assert ok
    assert await active_signup_count(db, shift.id) == 2

    # Third signup exceeds capacity.
    ok, msg = await signup_student(db, shift, s3.id)
    assert not ok
    assert "full" in msg.lower()
    assert await remaining_capacity(db, shift) == 0


async def test_unlimited_capacity(db, make_student, make_opportunity, make_shift):
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=0)
    s1 = await make_student(code="aaaa1111")
    await signup_student(db, shift, s1.id)
    assert await remaining_capacity(db, shift) is None


async def test_duplicate_signup_rejected(db, make_student, make_opportunity, make_shift):
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=5)
    s1 = await make_student(code="aaaa1111")
    ok, _ = await signup_student(db, shift, s1.id)
    assert ok
    ok, msg = await signup_student(db, shift, s1.id)
    assert not ok
    assert "already" in msg.lower()


async def test_cancel_frees_a_slot_and_allows_resignup(db, make_student, make_opportunity, make_shift):
    opp = await make_opportunity()
    shift = await make_shift(opp.id, capacity=1)
    s1 = await make_student(name="A", code="aaaa1111")
    s2 = await make_student(name="B", code="bbbb2222")

    await signup_student(db, shift, s1.id)
    ok, _ = await signup_student(db, shift, s2.id)
    assert not ok  # full

    signup1 = await get_signup(db, shift.id, s1.id)
    await cancel_signup(db, signup1)
    assert signup1.status == SignupStatus.cancelled
    assert await active_signup_count(db, shift.id) == 0

    # Now s2 fits, and s1 can re-sign up (reactivating the cancelled row).
    ok, _ = await signup_student(db, shift, s2.id)
    assert ok
    ok, msg = await signup_student(db, shift, s1.id)
    assert not ok and "full" in msg.lower()
