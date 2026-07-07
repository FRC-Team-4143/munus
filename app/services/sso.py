"""
SSO identity — the signed `mw_sso` browser cookie shared across MARS/WARS apps.

Legion mints `mw_sso` once a member approves a Slack push; every sibling app verifies it
locally with the shared `sso_secret` — no callback to Legion needed. Munus is a
*consumer*: it only ever **verifies** the cookie (it never mints one), so this is the
verify half of Legion's `services/sso.py`. Single sign-out is just a redirect to
Legion's `/sso/logout`.

Unlike Tempus (SSO-gated `/admin` only), Munus's student portal is gated by this same
cookie too — there is exactly one identity mechanism for the whole app. See
`services/legion_auth.py` for the one-tap challenge that gets a *fresh* browser onto
this cookie without making a student type their Legion username.

Claims carried by the cookie (see Legion's `make_sso_token`):
    member_code, username, name, role, team_number, groups (list of slugs), slack_user_id
`/admin` is gated on the `munus-admin`/`munus-manager` slugs being present in `groups`;
the portal is gated on `role == "student"`.
"""
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

SSO_COOKIE = "mw_sso"

_sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")


def read_sso_token(token: Optional[str]) -> Optional[dict]:
    """The verified claims for a raw cookie value, or None if absent/invalid/expired."""
    if not token:
        return None
    try:
        return _sso_signer.loads(token, max_age=settings.sso_session_ttl)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def sso_identity(request: Request) -> Optional[dict]:
    """The verified SSO claims for the current request, or None if absent/invalid."""
    return read_sso_token(request.cookies.get(SSO_COOKIE))


def make_authorize_url(request: Request, *, return_to: Optional[str] = None) -> str:
    """Where to send an unauthenticated caller to sign in: Legion's `/sso/authorize`.

    Defaults `return_to` to the current page (mirrors Tempus, right for `/admin`'s
    "you hit a protected page cold" case); pass it explicitly for the portal's `/enter`
    bootstrap, which wants to land somewhere other than `/enter` itself.
    """
    target = return_to if return_to is not None else str(request.url)
    return f"{settings.legion_base_url}/sso/authorize?app=munus&return_to={quote(target, safe='')}"


def logout_url(request: Request, *, return_to: str = "/") -> str:
    """Legion's single-logout endpoint, returning to `return_to` (a Munus path) afterward."""
    base = f"{request.url.scheme}://{request.url.netloc}{return_to}"
    return f"{settings.legion_base_url}/sso/logout?return_to={quote(base, safe='')}"


# ── Open-redirect guard (mirrors Legion's `allowed_return_to`) ────────────────────

def is_same_app_path(url: Optional[str]) -> bool:
    """True only for a safe same-app relative path (leading '/', not protocol-relative)."""
    if not url:
        return False
    parsed = urlparse(url)
    return not parsed.netloc and url.startswith("/") and not url.startswith("//")
