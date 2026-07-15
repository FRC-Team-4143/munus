from app.config import settings
from app.models import Opportunity, SignupStatus
from app.services.opportunities import (
    active_signup_count, cancel_signup, get_signup, opportunity_announcement_blocks,
    remaining_capacity, signup_student,
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
