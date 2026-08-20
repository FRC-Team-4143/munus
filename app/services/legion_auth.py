"""
One-tap sign-in for a member Munus already knows about (from a Slack payload), skipping
Legion's username-entry form. Two mechanisms live here:

**Magic links (`make_link_url`) — preferred for anything we send into Slack.** Slack's
in-app browser (iOS and Android both) throws away cookies between opens, so a member
who signed in a minute ago arrives with no `mw_sso` and would face a fresh Approve/Deny
push on every single tap. A signed link carries the identity itself, so the cookie
never has to survive. See Legion's `services/sso.make_link_token`.

**Challenges (`start_challenge`) — the older Slack-push round trip.** Still used by
`/enter` (`app/routers/portal.py`) for links already sitting in Slack history from
before magic links existed, and as the path for anyone arriving without a token. See
Legion's `routers/sso.py` `POST /sso/challenge`.
"""
import logging
from typing import Optional
from urllib.parse import quote

import httpx
from itsdangerous import URLSafeTimedSerializer

from app.config import settings

log = logging.getLogger(__name__)

# Same shared `sso_secret` Munus already uses to verify `mw_sso`, but a distinct salt —
# Legion's `read_link_token` will only accept tokens signed this way, and a link token
# must never be usable as a cookie value (it travels in a URL and lands in Slack
# history and browser history). Kept byte-identical to Legion's `_link_signer`.
_link_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso-link")


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


def make_link_url(member_code: str, next_path: str = "/me") -> str:
    """A one-tap magic-link URL signing `member_code` in to Munus at `next_path`.

    Use this for every link Munus puts into a Slack DM or ephemeral reply: Slack has
    already authenticated the recipient of those, so the Approve/Deny push was
    re-proving what Slack just told us — and doing it circularly, sending someone who
    is already in Slack to a page telling them to go back to Slack.

    `return_to` must be **absolute**: Legion's `/sso/link` redirects to it as-is, and a
    bare path would resolve against Legion's own host rather than Munus's (same trap
    `start_challenge` callers hit — see `/enter`'s docstring).

    Only send these over channels scoped to one person. A link is a bearer credential;
    in a shared channel anyone could redeem it as its addressee.
    """
    target = f"{settings.base_url}{safe_next(next_path)}"
    token = _link_signer.dumps({"member_code": member_code, "return_to": target})
    return f"{settings.legion_base_url}/sso/link?token={quote(token, safe='')}"


def safe_next(path: Optional[str]) -> str:
    """Only allow local, single-slash-rooted redirect targets (no open redirects).

    Rejects a leading `//` (protocol-relative) and a leading `/\\` — some browsers
    normalize the backslash to `/`, turning `/\\evil.com` into `//evil.com` and
    bypassing the plain `//` check (mirrors Legion's `allowed_return_to`)."""
    if (
        path
        and path.startswith("/")
        and not path.startswith("//")
        and not path.startswith("/\\")
    ):
        return path
    return "/"
