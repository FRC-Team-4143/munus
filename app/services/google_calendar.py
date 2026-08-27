"""
Google Calendar sync — one blocked-out event per Shift (opportunity name + time only,
no description/location/attendees) on a shared team calendar. Uses a service-account
credential (no OAuth flow, no stored user tokens). Mirrors slack_client.py's shape: a
lazy singleton client, thin async wrappers, exceptions caught and logged rather than
raised — a Calendar outage must never block an admin action. The Google client library
is synchronous, so every blocking call runs via asyncio.to_thread.
"""
import asyncio
import logging
from typing import Optional

from app.config import settings
from app.models import Shift

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
_service = None


def _configured() -> bool:
    return bool(settings.google_calendar_id and settings.google_service_account_file)


def _get_service():
    global _service
    if _service is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            settings.google_service_account_file, scopes=_SCOPES
        )
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _event_body(shift: Shift) -> dict:
    return {
        "summary": shift.opportunity.name,
        "start": {"dateTime": shift.start_time.isoformat() + "Z"},
        "end": {"dateTime": shift.end_time.isoformat() + "Z"},
    }


async def create_event(db, shift: Shift) -> Optional[str]:
    """Create this shift's calendar event and remember its id on the shift. Returns the
    event id, or None if sync isn't configured or the call failed."""
    if not _configured():
        return None
    try:
        service = _get_service()
        body = _event_body(shift)
        event = await asyncio.to_thread(
            lambda: service.events()
            .insert(calendarId=settings.google_calendar_id, body=body)
            .execute()
        )
    except Exception:
        log.exception("Failed to create calendar event for shift %s", shift.id)
        return None
    shift.google_event_id = event["id"]
    await db.commit()
    return event["id"]


async def update_event(db, shift: Shift) -> bool:
    """Push this shift's current name/time to its already-created calendar event.
    Returns False if sync isn't configured, the shift has no event yet, or the call
    failed."""
    if not _configured() or not shift.google_event_id:
        return False
    try:
        service = _get_service()
        body = _event_body(shift)
        await asyncio.to_thread(
            lambda: service.events()
            .update(calendarId=settings.google_calendar_id, eventId=shift.google_event_id, body=body)
            .execute()
        )
        return True
    except Exception:
        log.exception("Failed to update calendar event for shift %s", shift.id)
        return False


async def delete_event(db, shift: Shift) -> bool:
    """Delete this shift's calendar event and clear the stored id. Returns False if
    sync isn't configured or the shift has no event. An already-gone event (404/410)
    still counts as success."""
    if not _configured() or not shift.google_event_id:
        return False
    from googleapiclient.errors import HttpError

    service = _get_service()
    event_id = shift.google_event_id
    try:
        await asyncio.to_thread(
            lambda: service.events()
            .delete(calendarId=settings.google_calendar_id, eventId=event_id)
            .execute()
        )
    except HttpError as e:
        if e.resp.status not in (404, 410):
            log.exception("Failed to delete calendar event for shift %s", shift.id)
            return False
    except Exception:
        log.exception("Failed to delete calendar event for shift %s", shift.id)
        return False
    shift.google_event_id = None
    await db.commit()
    return True
