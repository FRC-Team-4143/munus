"""
Legion roster sync — pulls the source-of-truth roster from Legion's read-only API and
upserts it into Munus's local mirror (`Student`/`Mentor`).

Data flows one way: Legion -> Munus. Munus never writes roster data back. Members are
keyed on Legion's stable `member_code`; existing local rows created before the cutover
are back-linked by `slack_user_id` (unique) then by exact name on first sync.
Incremental syncs pass `updated_since` (the previous sync's start time) so only changed
members are fetched.

Unlike Tempus, Munus has no `Team`/`Subteam` mirror tables — it only ever needed the raw
`team_number` int (no team name, no subteam concept) — so there's nothing to upsert but
members. A student's season requirement pool (`Student.level`) is *derived* from the
synced `grade` + `team_number` on every sync (`services.requirements.derive_level`)
rather than being admin-set, so it stays in lockstep with Legion without a separate
write path.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Mentor, Student
from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting, set_setting
from app.services.requirements import derive_level

log = logging.getLogger(__name__)


class LegionSyncError(RuntimeError):
    """Raised when the sync can't run (misconfigured or Legion unreachable)."""


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    resp = await client.get(path, params={k: v for k, v in params.items() if v is not None})
    resp.raise_for_status()
    return resp.json()


async def sync_roster(db: AsyncSession, *, full: bool = False) -> str:
    """Pull members from Legion and upsert the local Student/Mentor mirror.
    Pass `full=True` to ignore the incremental watermark and re-pull everyone.
    Returns a short human summary. Raises `LegionSyncError` on config/transport failure."""
    if not settings.legion_base_url or not settings.legion_api_key:
        raise LegionSyncError("Legion is not configured (set LEGION_BASE_URL and LEGION_API_KEY).")

    sync_start = datetime.utcnow().isoformat()
    since = None if full else await get_setting(db, LEGION_LAST_SYNCED_KEY)
    headers = {"X-API-Key": settings.legion_api_key}
    try:
        async with httpx.AsyncClient(
            base_url=settings.legion_base_url, headers=headers, timeout=30
        ) as client:
            members = (await _get(client, "/api/members", updated_since=since))["members"]
    except (httpx.HTTPError, KeyError) as e:
        raise LegionSyncError(f"Legion API request failed: {e}") from e

    counts = await _upsert_members(db, members)

    # Watermark = this sync's start; a member changed mid-sync is re-pulled next time (>=).
    await set_setting(db, LEGION_LAST_SYNCED_KEY, sync_start)  # commits
    summary = f"{counts['students']} students, {counts['mentors']} mentors"
    log.info("Legion sync complete: %s (since=%s)", summary, since or "full")
    return summary


async def _find_local(db: AsyncSession, model, member: dict):
    """Locate the local row for a Legion member: by member_code, else back-link by
    slack_user_id, else by exact (case-insensitive) name. Returns the row or None."""
    code = member["member_code"]
    row = (await db.execute(select(model).where(model.member_code == code))).scalars().first()
    if row:
        return row
    slack_id = member.get("slack_user_id")
    if slack_id:
        row = (await db.execute(
            select(model).where(model.slack_user_id == slack_id)
        )).scalars().first()
        if row:
            return row
    return (await db.execute(
        select(model).where(func.lower(model.name) == member["name"].lower())
    )).scalars().first()


async def _upsert_members(db: AsyncSession, members: list[dict]) -> dict:
    counts = {"students": 0, "mentors": 0}

    for m in members:
        is_student = m["role"] == "student"
        model = Student if is_student else Mentor
        team_number = m.get("team_number")

        row = await _find_local(db, model, m)
        if row is None:
            row = model(member_code=m["member_code"])
            db.add(row)
        row.member_code = m["member_code"]
        row.name = m["name"]
        row.slack_user_id = m.get("slack_user_id")
        row.is_active = m["is_active"]
        row.archived_at = None if m["is_active"] else (row.archived_at or datetime.utcnow())

        if is_student:
            row.team_number = team_number
            row.grade = m.get("grade")
            row.level = derive_level(row.grade, team_number)

        counts["students" if is_student else "mentors"] += 1

    return counts
