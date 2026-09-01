"""
Opportunity / shift signup logic — capacity checks and signup/cancel.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Opportunity, Shift, Signup, SignupStatus, Student
from app.services.google_calendar import create_event, delete_event, update_event
from app.services.slack_client import post_to_channel, update_channel_message
from app.utils import format_date_range, format_shift_range, now_utc


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
) -> list[dict]:
    """A few active opportunities the student could sign up for next: not already
    signed up for, and either continuous (always open) or with at least one shift that
    hasn't ended yet. Soonest upcoming shift first, continuous opportunities last (they
    have no date to sort by). Used to nudge a student who's still short of their season
    requirement even after their upcoming shifts (`reports.student_vhours_message`).

    Each result is `{"opp": Opportunity, "date_range": str}` — the compact
    `format_date_range` span of the opportunity's remaining shifts, or "Ongoing" for a
    continuous opportunity (it has no shifts to date)."""
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
    candidates: list[tuple[Optional[datetime], Opportunity, str]] = []
    for opp in opps:
        if opp.id in signed_up_opp_ids:
            continue
        if opp.is_continuous:
            candidates.append((None, opp, "Ongoing"))
            continue
        upcoming = [s for s in opp.shifts if s.start_time > now or s.end_time > now]
        if upcoming:
            candidates.append((min(s.start_time for s in upcoming), opp, format_date_range(upcoming)))

    candidates.sort(key=lambda triple: (triple[0] is None, triple[0] or now))
    return [{"opp": opp, "date_range": date_range} for _, opp, date_range in candidates[:limit]]


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


async def signed_up_students(db: AsyncSession, shift_ids: list[int]) -> list[Student]:
    """Distinct students currently signed up (not cancelled) for any of the given
    shifts, filtered to those with a linked Slack account. Shared by the admin's
    Notify and custom-Message DM actions."""
    if not shift_ids:
        return []
    signups = (
        await db.execute(
            select(Signup)
            .options(selectinload(Signup.student))
            .where(Signup.shift_id.in_(shift_ids), Signup.status == SignupStatus.signed_up)
        )
    ).scalars().all()
    seen: dict[int, Student] = {}
    for su in signups:
        if su.student and su.student.slack_user_id:
            seen[su.student_id] = su.student
    return list(seen.values())


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

    The button is an **interactive** button (`action_id: opportunity_view`), not a link
    button, and clicking it opens a modal — `opportunity_signup_modal` for a shift-based
    opportunity, or `opportunity_log_hours_modal` for a continuous one (its button reads
    "📝 View & record hours" instead, since there's nothing to sign up for) — see there
    for why a shared-channel message can't personalize a `url` button, and why a modal
    rather than a reply.

    It was briefly a plain `url` button straight to the opportunity page, on the
    reasoning that it was one tap for anyone holding a live session and only cost the
    sign-in wall otherwise. That traded on the session surviving — and it never does:
    Slack's in-app browser discards cookies between opens, so *every* click paid the
    wall, in its worst form (the link carried no `member`, so it landed on Legion's
    type-your-username form rather than even a one-tap push).

    Routing note: `opportunity_view` *and* the modal's `opportunity_signup` callback
    must stay registered in Legion's `routers/slack_dispatch.py` — unrouted ids are
    swallowed with a 200, which makes the button look broken rather than error."""
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

    # Blank lines between groups (required/continuous flag, description, bullets) so
    # Slack renders them as separate paragraphs instead of one dense block; single "\n" within a
    # group keeps its lines (e.g. the info bullets) tight against each other.
    groups = []
    if opp.is_required:
        # is_required is only ever True for a shift-based opportunity — routers/admin.py
        # normalizes it to False for a continuous one on both create and edit, so this
        # copy never needs continuous-specific wording.
        groups.append("🚨 *Required — every active student must sign up for at least 1 shift.*")
    if opp.is_continuous:
        groups.append("🔄 *Ongoing — no shifts here, log hours anytime you help out.*")
    if opp.description:
        groups.append(opp.description)
    if info:
        groups.append("\n".join(info))
    body = "\n\n".join(groups)
    text = f"{title}\n\n{body}" if body else title

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}]
    if body:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    button_text = "📝 View & record hours" if opp.is_continuous else "🙋 View & sign up"
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": button_text, "emoji": True},
                    "action_id": "opportunity_view",
                    "value": str(opp.id),
                }
            ],
        }
    )
    return text, blocks


SIGNUP_CALLBACK = "opportunity_signup"
LOG_HOURS_CALLBACK = "opportunity_log_hours"
_SELECT_OPTION_MAX = 75  # Slack's hard cap on a select option's text


def _shift_option(row: dict) -> dict:
    """One `static_select` option for a shift row from `shift_options_for_modal`."""
    label = format_shift_range(row["shift"].start_time, row["shift"].end_time)
    if row["signed_up"]:
        label += " ✅ signed up"
    elif row["is_full"]:
        label += " · FULL"
    elif row["remaining"] is not None:
        label += f" · {row['remaining']} left"
    if len(label) > _SELECT_OPTION_MAX:
        label = label[: _SELECT_OPTION_MAX - 1] + "…"
    return {
        "text": {"type": "plain_text", "text": label},
        "value": str(row["shift"].id),
    }


def opportunity_signup_modal(
    opp: Opportunity,
    shift_rows: Optional[list[dict]],
    *,
    notice: Optional[str] = None,
    details_url: Optional[str] = None,
) -> dict:
    """The modal behind the announcement's "🙋 View & sign up" button.

    Opening a modal rather than replying with a message is the whole point. A click in a
    shared channel identifies the clicker — which a plain `url` button never can, since
    Slack renders it client-side and it never reaches us — but *any* reply, even an
    ephemeral one, puts another message in the channel. A modal opens in place and posts
    nothing, so the button just works.

    `notice` renders instead of the shift picker and suppresses the submit button, for
    the cases where there's nothing to sign up for: one with no upcoming shifts, or a
    mentor (a read-only viewer here, same as on the web). A continuous opportunity never
    reaches this modal — `_handle_opportunity_view` routes it to
    `opportunity_log_hours_modal` instead, since logging hours directly *is* the action
    there.

    `details_url` links out to the full opportunity page, for what the modal can't hold:
    who else is signed up, and cancelling a signup. It's a per-person magic link, which
    is safe *here* for the same reason it isn't in the announcement — a modal is opened
    by and shown to exactly one person. Rendered as a section link rather than a `url`
    button on purpose: Slack sends an interaction payload for url buttons that has to be
    acked, and a plain link needs our server not at all.

    A student can join more than one shift on the same opportunity — `signup_student`
    only blocks a duplicate on the *same* shift, and the channel button stays tappable
    after a signup, not consumed by one. `shift_rows` (from `shift_options_for_modal`)
    already flags each already-joined shift with `signed_up`; when any are, this calls
    that out by name above the picker rather than just marking it "✅ signed up" among
    the options, where it's easy to miss before picking a second one.
    """
    blocks: list[dict] = []
    details = []
    if opp.is_required:
        details.append("🚨 *Required — every active student must sign up for at least 1 shift.*")
    if opp.description:
        details.append(opp.description)
    info = []
    if opp.location:
        info.append(f"📍 {opp.location}")
    if opp.attire:
        info.append(f"👕 {opp.attire}")
    if info:
        details.append("\n".join(info))
    if details:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(details)}})

    joined = [r for r in (shift_rows or []) if r["signed_up"]]
    if joined:
        ranges = "\n".join(
            f"• {format_shift_range(r['shift'].start_time, r['shift'].end_time)}" for r in joined
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"✅ *You're already signed up for:*\n{ranges}"},
        })

    if notice:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": notice}})
    else:
        blocks.append({
            "type": "input",
            "block_id": "shift",
            "label": {"type": "plain_text", "text": "Pick a shift"},
            "element": {
                "type": "static_select",
                "action_id": "value",
                "placeholder": {"type": "plain_text", "text": "Choose a shift"},
                "options": [_shift_option(r) for r in shift_rows or []],
            },
        })

    if details_url:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{details_url}|🔗 Full details, and who else is signed up>",
            },
        })

    title = opp.name if len(opp.name) <= 24 else opp.name[:23] + "…"  # Slack caps titles at 24
    view = {
        "type": "modal",
        "callback_id": SIGNUP_CALLBACK,
        "private_metadata": str(opp.id),
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }
    if not notice:
        view["submit"] = {"type": "plain_text", "text": "Sign up"}
    return view


def opportunity_log_hours_modal(opp: Opportunity, *, details_url: Optional[str] = None) -> dict:
    """The modal behind a continuous opportunity's "📝 View & record hours" button.

    A continuous opportunity has no shifts to pick, so unlike `opportunity_signup_modal`
    this skips straight to an hours + notes form — logging hours directly *is* the
    action here. Shape mirrors `submissions.log_hours_modal`. Submission is handled by
    `routers.slack._handle_opportunity_log_hours_submit`, which calls the same
    `submissions.submit_opportunity_hours` the web `/opportunities/{id}/log-hours` form
    does, so reviewer routing can't drift between the two.

    No `is_required` flag here — `routers/admin.py` normalizes that field to False for
    any continuous opportunity on both create and edit, so one can never actually be
    required.
    """
    details = []
    if opp.description:
        details.append(opp.description)
    info = []
    if opp.location:
        info.append(f"📍 {opp.location}")
    if opp.attire:
        info.append(f"👕 {opp.attire}")
    if info:
        details.append("\n".join(info))

    blocks: list[dict] = []
    if details:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(details)}})
    blocks.append({
        "type": "input",
        "block_id": "hours",
        "label": {"type": "plain_text", "text": "Hours volunteered"},
        "element": {"type": "plain_text_input", "action_id": "value"},
    })
    blocks.append({
        "type": "input",
        "block_id": "report",
        "optional": True,
        "label": {"type": "plain_text", "text": "What did you do? (optional)"},
        "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
    })
    if details_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{details_url}|🔗 Full details>"},
        })

    title = opp.name if len(opp.name) <= 24 else opp.name[:23] + "…"  # Slack caps titles at 24
    return {
        "type": "modal",
        "callback_id": LOG_HOURS_CALLBACK,
        "private_metadata": str(opp.id),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Log hours"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


async def shift_options_for_modal(
    db: AsyncSession, opp: Opportunity, student_id: Optional[int]
) -> list[dict]:
    """Joinable shifts for `opp`, with capacity and already-signed-up state.

    Mirrors the opportunity page's own filter (`routers/portal.py`): a shift stays
    joinable until it's fully over, so one already in progress doesn't vanish from the
    list the moment it starts."""
    now = now_utc()
    shifts = sorted(
        [s for s in opp.shifts if s.start_time > now or s.end_time > now],
        key=lambda s: s.start_time,
    )
    mine: set[int] = set()
    if student_id is not None and shifts:
        mine = {
            row.shift_id
            for row in (
                await db.execute(
                    select(Signup).where(
                        Signup.student_id == student_id,
                        Signup.status == SignupStatus.signed_up,
                        Signup.shift_id.in_([s.id for s in shifts]),
                    )
                )
            ).scalars().all()
        }

    rows = []
    for shift in shifts:
        remaining = await remaining_capacity(db, shift)
        rows.append({
            "shift": shift,
            "remaining": remaining,
            "is_full": remaining is not None and remaining <= 0,
            "signed_up": shift.id in mine,
        })
    return rows


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


async def sync_shift_calendar_event(db: AsyncSession, shift: Shift) -> None:
    """Create or update this shift's Google Calendar event (opportunity name + time
    only). `shift.opportunity` must already be loaded. No-op if calendar sync isn't
    configured — see services/google_calendar.py."""
    if shift.google_event_id:
        await update_event(db, shift)
    else:
        await create_event(db, shift)


async def remove_shift_calendar_event(db: AsyncSession, shift: Shift) -> None:
    """Delete this shift's Google Calendar event, if any. No-op if calendar sync isn't
    configured — see services/google_calendar.py."""
    await delete_event(db, shift)
