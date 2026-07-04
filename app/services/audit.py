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


async def record(
    db: AsyncSession,
    request: Optional[Request],
    action: str,
    summary: str,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    detail: Optional[dict] = None,
    actor: str = "admin",
) -> None:
    """Stage an audit row on the session (does not commit).

    `action` is a dotted verb like "submission.approve"; `detail` is an optional dict
    serialized to JSON.
    """
    db.add(AuditLog(
        timestamp=datetime.utcnow(),
        actor=actor,
        ip=_client_ip(request),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary,
        detail=json.dumps(detail, default=str) if detail else None,
    ))
