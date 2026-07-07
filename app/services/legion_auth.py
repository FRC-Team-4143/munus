"""
One-tap sign-in — starts a Legion SSO challenge for a member Munus already knows about
(from a Slack payload), skipping Legion's username-entry form.

This is the server-to-server half of the flow; `app/routers/portal.py`'s `/enter` route
is the other half (decides whether a challenge is even needed, and sends the browser to
the pending-page URL this returns). See Legion's `routers/sso.py` `POST /sso/challenge`
docstring for the full round trip.
"""
import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def start_challenge(member_code: str, *, return_to: str = "/") -> Optional[str]:
    """POST Legion's /sso/challenge for `member_code`. Returns the `/sso/pending/{nonce}`
    URL the browser should be sent to (it sends the Slack Approve/Deny push as a side
    effect), or None if Legion is unreachable/misconfigured/rate-limited — the caller
    should show a "sign-in temporarily unavailable" page rather than crash."""
    if not settings.legion_base_url or not settings.legion_api_key:
        log.error("Cannot start a Legion SSO challenge: LEGION_BASE_URL/LEGION_API_KEY not set.")
        return None

    headers = {"X-API-Key": settings.legion_api_key}
    try:
        async with httpx.AsyncClient(
            base_url=settings.legion_base_url, headers=headers, timeout=10
        ) as client:
            resp = await client.post(
                "/sso/challenge",
                json={"member_code": member_code, "app": "munus", "return_to": return_to},
            )
            resp.raise_for_status()
            nonce = resp.json()["nonce"]
    except (httpx.HTTPError, KeyError) as e:
        log.error("Legion SSO challenge failed for %s: %s", member_code, e)
        return None

    return f"{settings.legion_base_url}/sso/pending/{nonce}"


def safe_next(path: Optional[str]) -> str:
    """Only allow local, single-slash-rooted redirect targets (no open redirects)."""
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return "/"
