"""
Opportunity / shift signup logic — capacity checks and signup/cancel.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Opportunity, Shift, Signup, SignupStatus
from app.services.slack_client import post_to_channel
from app.utils import now_utc


async def upcoming_signups_for_student(db: AsyncSession, student_id: int) -> list[Signup]:
    """A student's signed-up shifts that haven't ended yet, soonest first, with the
    shift's opportunity eager-loaded. Shared by the dashboard and the `/vhours` command."""
    return (
        await db.execute(
            select(Signup)
            .options(selectinload(Signup.shift).selectinload(Shift.opportunity))
            .join(Shift, Shift.id == Signup.shift_id)
            .where(
                Signup.student_id == student_id,
                Signup.status == SignupStatus.signed_up,
                Shift.end_time >= now_utc(),
            )
            .order_by(Shift.start_time)
        )
    ).scalars().all()


async def active_signup_count(db: AsyncSession, shift_id: int) -> int:
    """Number of students currently signed up (not cancelled) for a shift."""
    result = await db.execute(
        select(func.count())
        .select_from(Signup)
        .where(Signup.shift_id == shift_id, Signup.status == SignupStatus.signed_up)
    )
    return int(result.scalar() or 0)


async def remaining_capacity(db: AsyncSession, shift: Shift) -> Optional[int]:
    """Remaining open slots for a shift, or None when the shift is unlimited (capacity 0)."""
    if not shift.capacity:
        return None
    taken = await active_signup_count(db, shift.id)
    return max(0, shift.capacity - taken)


async def get_signup(db: AsyncSession, shift_id: int, student_id: int) -> Optional[Signup]:
    return (
        await db.execute(
            select(Signup).where(
                Signup.shift_id == shift_id, Signup.student_id == student_id
            )
        )
    ).scalars().first()


async def signup_student(db: AsyncSession, shift: Shift, student_id: int) -> tuple[bool, str]:
    """Sign a student up for a shift. Returns (ok, message). Enforces capacity and
    re-activates a previously cancelled signup rather than creating a duplicate."""
    existing = await get_signup(db, shift.id, student_id)
    if existing and existing.status == SignupStatus.signed_up:
        return False, "You're already signed up for this shift."

    remaining = await remaining_capacity(db, shift)
    if remaining is not None and remaining <= 0:
        return False, "This shift is full."

    if existing:
        existing.status = SignupStatus.signed_up
        existing.created_at = datetime.utcnow()
        existing.reminded_at = None
        existing.prompted_at = None
    else:
        db.add(Signup(shift_id=shift.id, student_id=student_id, status=SignupStatus.signed_up))
    await db.commit()
    return True, "You're signed up!"


async def cancel_signup(db: AsyncSession, signup: Signup) -> None:
    signup.status = SignupStatus.cancelled
    await db.commit()


def opportunity_announcement_blocks(opp: Opportunity) -> tuple[str, list]:
    """Build the (fallback text, blocks) for a new-opportunity channel announcement.

    The button is a plain Slack *link* button (a `url`, no `action_id`) straight to the
    opportunity page — it never touches our server, so it's a real one-tap click for
    anyone with a live Legion session. There's no way to personalize a shared channel
    message's button per-clicker, so someone without a live session just hits Munus's
    normal sign-in wall (types their username) instead of the one-tap Slack-push
    bootstrap `/enter` gives you — a deliberate trade for not needing a second,
    ephemeral reply message just to open the page."""
    lines = [f"✨ *New volunteer opportunity: {opp.name}*"]
    if opp.description:
        lines.append(opp.description)
    if opp.location:
        lines.append(f"📍 {opp.location}")
    if opp.attire:
        lines.append(f"👕 {opp.attire}")
    text = "\n".join(lines)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🙋 View & sign up", "emoji": True},
                    "url": f"{settings.base_url}/opportunities/{opp.id}",
                }
            ],
        },
    ]
    return text, blocks


async def announce_opportunity(opp: Opportunity) -> Optional[str]:
    """Post a new-opportunity announcement to the configured Slack channel. No-op when
    SLACK_ANNOUNCE_CHANNEL is blank. Returns the message ts or None."""
    if not settings.slack_announce_channel:
        return None
    text, blocks = opportunity_announcement_blocks(opp)
    return await post_to_channel(settings.slack_announce_channel, text, blocks=blocks, automated=True)
