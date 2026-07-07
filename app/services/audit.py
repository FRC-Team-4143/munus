"""
Audit logging — append-only record of admin/reviewer mutations.

Call `record(...)` inside a request handler, before `db.commit()`, so the audit row
is committed in the same transaction as the change it describes.
"""
import json
from datetime import datetime
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None or request.client is None:
        return None
    return request.client.host


def _actor_from_request(request: Optional[Request]) -> str:
    """The signed-in admin/manager's identity for the actor column: their SSO username
    (or name), falling back to "system" when there's no verified identity (e.g.
    scheduled jobs)."""
    if request is None:
        return "system"
    from app.services.sso import sso_identity
    identity = sso_identity(request)
    if identity is None:
        return "system"
    return identity.get("username") or identity.get("name") or "system"


async def record(
    db: AsyncSession,
    request: Optional[Request],
    action: str,
    summary: str,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    detail: Optional[dict] = None,
    actor: Optional[str] = None,
) -> None:
    """Stage an audit row on the session (does not commit).

    `action` is a dotted verb like "submission.approve"; `detail` is an optional dict
    serialized to JSON. `actor` defaults to the SSO identity on the request (see
    `_actor_from_request`).
    """
    db.add(AuditLog(
        timestamp=datetime.utcnow(),
        actor=actor or _actor_from_request(request),
        ip=_client_ip(request),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary,
        detail=json.dumps(detail, default=str) if detail else None,
    ))
