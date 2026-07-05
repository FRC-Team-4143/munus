"""
Student portal identity — session cookie + Slack magic-link tokens.

Two separate signed tokens, both keyed off `session_secret`:
  - Session cookie (`munus_student`): long-lived, set once the student is identified.
  - Magic-link token: short-lived, embedded in the `/enter?token=...` link that Slack
    hands out so a student can sign in with one tap and no password.

Kept in a service (not a router) so the portal and Slack routers share it without
importing each other.
"""
from typing import Optional
from urllib.parse import quote

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer

from app.config import settings

# ── Session cookie ─────────────────────────────────────────────────────────────

SESSION_COOKIE = "munus_student"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_session_signer = URLSafeSerializer(settings.session_secret, salt="student-session")


def set_session_cookie(response, student_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, _session_signer.dumps(student_id),
        httponly=True, samesite="lax", max_age=SESSION_MAX_AGE,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def student_id_from_session(request: Request) -> Optional[int]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return int(_session_signer.loads(token))
    except (BadSignature, ValueError, TypeError):
        return None


# ── Magic link (Slack one-tap sign-in) ─────────────────────────────────────────

MAGIC_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

_magic_signer = URLSafeTimedSerializer(settings.session_secret, salt="student-magic")


def make_magic_token(student_id: int) -> str:
    return _magic_signer.dumps(student_id)


def read_magic_token(token: str) -> Optional[int]:
    """Return the student id encoded in a magic-link token, or None if invalid/expired."""
    try:
        return int(_magic_signer.loads(token, max_age=MAGIC_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def safe_next(path: Optional[str]) -> str:
    """Only allow local, single-slash-rooted redirect targets (no open redirects)."""
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return "/"


def magic_link(student_id: int, next_path: str = "/") -> str:
    """Absolute one-tap sign-in URL for a student (uses BASE_URL).

    `next_path` is where the student lands after sign-in (defaults to the dashboard).
    """
    base = settings.base_url.rstrip("/")
    url = f"{base}/enter?token={make_magic_token(student_id)}"
    if next_path and next_path != "/":
        url += f"&next={quote(safe_next(next_path), safe='')}"
    return url
