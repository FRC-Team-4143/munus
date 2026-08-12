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
from app.services.slack_client import post_to_channel, update_channel_message
from app.utils import format_date_range, now_utc


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


async def available_opportunities_for_student(
    db: AsyncSession, student_id: int, limit: int = 3
) -> list[Opportunity]:
    """A few active opportunities the student could sign up for next: not already
    signed up for, and either continuous (always open) or with at least one shift that
    hasn't ended yet. Soonest upcoming shift first, continuous opportunities last (they
    have no date to sort by). Used to nudge a student who's still short of their season
    requirement even after their upcoming shifts (`reports.student_vhours_message`)."""
    opps = (
        await db.execute(
            select(Opportunity)
            .options(selectinload(Opportunity.shifts))
            .where(Opportunity.is_active.is_(True))
        )
    ).scalars().all()

    signed_up_opp_ids = {
        oid
        for (oid,) in (
            await db.execute(
                select(Shift.opportunity_id)
                .join(Signup, Signup.shift_id == Shift.id)
                .where(Signup.student_id == student_id, Signup.status == SignupStatus.signed_up)
            )
        ).all()
    }

    now = now_utc()
    candidates: list[tuple[Optional[datetime], Opportunity]] = []
    for opp in opps:
        if opp.id in signed_up_opp_ids:
            continue
        if opp.is_continuous:
            candidates.append((None, opp))
            continue
        upcoming = [s for s in opp.shifts if s.start_time > now or s.end_time > now]
        if upcoming:
            candidates.append((min(s.start_time for s in upcoming), opp))

    candidates.sort(key=lambda pair: (pair[0] is None, pair[0] or now))
    return [opp for _, opp in candidates[:limit]]


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

    `opp.shifts` must already be loaded (e.g. `selectinload(Opportunity.shifts)`) —
    this is a sync function and can't lazy-load across an async session. The date line
    it builds from those shifts is why every shift create/edit/delete route also calls
    `update_announcement` afterward (see routers/admin.py): the span can shift as shifts
    are added, rescheduled, or removed, and the already-posted message needs to track it.

    The button is a plain Slack *link* button (a `url`, no `action_id`) straight to the
    opportunity page — it never touches our server, so it's a real one-tap click for
    anyone with a live Legion session. There's no way to personalize a shared channel
    message's button per-clicker, so someone without a live session just hits Munus's
    normal sign-in wall (types their username) instead of the one-tap Slack-push
    bootstrap `/enter` gives you — a deliberate trade for not needing a second,
    ephemeral reply message just to open the page."""
    info = []
    if opp.location:
        info.append(f"📍 {opp.location}")
    date_range = format_date_range(opp.shifts)
    if date_range:
        info.append(f"📅 {date_range}")
    if opp.attire:
        info.append(f"👕 {opp.attire}")

    # The title gets its own `header` block — Slack renders it noticeably larger/bolder
    # than section text, which a mrkdwn size trick can't do. `header` is plain_text only
    # (no `*bold*` markup, capped at 150 chars) so it's built and truncated separately
    # from the mrkdwn groups below.
    title = f"✨ New volunteer opportunity: {opp.name}"
    if len(title) > 150:
        title = title[:149] + "…"

    # Blank lines between groups (required flag/description/bullets) so Slack renders
    # them as separate paragraphs instead of one dense block; single "\n" within a
    # group keeps its lines (e.g. the info bullets) tight against each other.
    groups = []
    if opp.is_required:
        groups.append("🚨 *Required — every active student must sign up for at least 1 shift.*")
    if opp.description:
        groups.append(opp.description)
    if info:
        groups.append("\n".join(info))
    body = "\n\n".join(groups)
    text = f"{title}\n\n{body}" if body else title

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}]
    if body:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🙋 View & sign up", "emoji": True},
                    "url": f"{settings.base_url}/opportunities/{opp.id}",
                }
            ],
        }
    )
    return text, blocks


async def announce_opportunity(db: AsyncSession, opp: Opportunity) -> Optional[str]:
    """Post a new-opportunity announcement to the configured Slack channel, and
    remember where (channel + ts) so a later edit can keep that message in sync via
    `update_announcement`. No-op when SLACK_ANNOUNCE_CHANNEL is blank. Returns the
    message ts or None."""
    if not settings.slack_announce_channel:
        return None
    text, blocks = opportunity_announcement_blocks(opp)
    ts = await post_to_channel(settings.slack_announce_channel, text, blocks=blocks, automated=True)
    if ts:
        opp.announcement_channel_id = settings.slack_announce_channel
        opp.announcement_ts = ts
        await db.commit()
    return ts


async def update_announcement(db: AsyncSession, opp: Opportunity) -> Optional[bool]:
    """Re-render the opportunity's current details and push them to its already-posted
    announcement message, if it has one — keeps an edit (name, description, location,
    attire, required status) in sync with what's pinned in Slack. Returns None (no-op)
    if the opportunity was never announced; otherwise True/False for the chat.update
    call's success, mirroring `update_channel_message`."""
    if not opp.announcement_channel_id or not opp.announcement_ts:
        return None
    text, blocks = opportunity_announcement_blocks(opp)
    return await update_channel_message(
        opp.announcement_channel_id, opp.announcement_ts, text, blocks=blocks
    )
