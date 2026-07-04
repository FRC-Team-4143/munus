"""
Runtime-configurable app settings backed by the `app_settings` key/value table.

Currently holds the optional `season_start` cutoff — the date the season's required
hours are counted from. A missing/blank value falls back to the SEASON_START env var;
if that is also blank, all approved hours count (no cutoff).
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSetting
from app.utils import local_to_utc

SEASON_START_KEY = "season_start"


async def get_setting(db: AsyncSession, key: str) -> Optional[str]:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    return row.value if row else None


async def set_setting(db: AsyncSession, key: str, value: Optional[str]) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def get_season_start(db: AsyncSession) -> Optional[date]:
    """Date the season required-hours total counts from, or None for 'count all'."""
    stored = await get_setting(db, SEASON_START_KEY)
    if stored is not None:
        # An explicitly-stored blank string means "no cutoff".
        return _parse_date(stored)
    return _parse_date(settings.season_start)


async def set_season_start(db: AsyncSession, value: Optional[date]) -> None:
    """Upsert the season_start row. None/blank clears the cutoff (count all)."""
    await set_setting(db, SEASON_START_KEY, value.isoformat() if value else "")


async def season_start_utc(db: AsyncSession) -> Optional[datetime]:
    """The season cutoff as a naive-UTC datetime for `submitted_at` comparisons, or None."""
    d = await get_season_start(db)
    if d is None:
        return None
    return local_to_utc(datetime.combine(d, datetime.min.time()))
