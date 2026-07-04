"""
Timezone helpers and shared date/time utilities.

All datetimes in the database are stored as naive UTC. These helpers convert
to/from the configured local timezone (default: America/New_York).
"""
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


_UTC = ZoneInfo("UTC")


def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive UTC datetime to a naive local datetime."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC).astimezone(_tz()).replace(tzinfo=None)


def local_to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive local datetime to a naive UTC datetime (for DB queries)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_tz()).astimezone(_UTC).replace(tzinfo=None)


def today_local() -> date:
    """Today's date in the local timezone."""
    return datetime.now(_tz()).date()


def now_utc() -> datetime:
    """Current moment as a naive UTC datetime (matches how the DB stores times)."""
    return datetime.utcnow()


def format_shift_range(start: datetime, end: Optional[datetime] = None) -> str:
    """Human-friendly local rendering of a shift's start (and optional end)."""
    start_local = utc_to_local(start)
    if end is None:
        return start_local.strftime("%a %b %d · %I:%M %p")
    end_local = utc_to_local(end)
    if start_local.date() == end_local.date():
        return (
            f"{start_local.strftime('%a %b %d · %I:%M %p')}"
            f" – {end_local.strftime('%I:%M %p')}"
        )
    return (
        f"{start_local.strftime('%a %b %d · %I:%M %p')}"
        f" – {end_local.strftime('%a %b %d · %I:%M %p')}"
    )


def current_week_bounds() -> tuple[datetime, datetime]:
    """Return (week_start_utc, week_end_utc) for the current Mon–Sun week."""
    week_start = today_local() - timedelta(days=today_local().weekday())
    week_end = week_start + timedelta(days=7)
    return (
        local_to_utc(datetime.combine(week_start, datetime.min.time())),
        local_to_utc(datetime.combine(week_end, datetime.min.time())),
    )
