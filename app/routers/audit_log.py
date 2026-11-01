"""Super Admin: query durable audit log for doctors / centres / centre users."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

try:
    from app.database import get_db
    from app.models import AuditLogDB, UserDB
    from app.security import require_roles
except ImportError:
    from app.database import get_db
    from app.models import AuditLogDB, UserDB
    from app.security import require_roles

router = APIRouter(prefix="/audit-log", tags=["Audit Log"])


def _row(a: AuditLogDB) -> dict:
    return {
        "id": a.id,
        "at": a.at,
        "actorUserId": a.actor_user_id,
        "actorName": a.actor_name,
        "actorRole": a.actor_role,
        "entityType": a.entity_type,
        "entityId": a.entity_id,
        "action": a.action,
        "before": a.before,
        "after": a.after,
        "approvalId": a.approval_id,
        "note": a.note,
    }


@router.get("")
def list_audit_log(
    entityType: Optional[str] = Query(None),
    entityId: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _u: UserDB = Depends(require_roles("SUPER_ADMIN")),
) -> List[dict]:
    try:
        from app.entity_lifecycle import ensure_freeze_and_audit_tables
        from app.database import engine
        ensure_freeze_and_audit_tables(engine)
    except Exception:
        pass
    q = db.query(AuditLogDB)
    if entityType:
        q = q.filter(AuditLogDB.entity_type == entityType)
    if entityId:
        q = q.filter(AuditLogDB.entity_id == str(entityId))
    if action:
        q = q.filter(AuditLogDB.action == action.upper())
    rows = q.order_by(AuditLogDB.at.desc()).limit(limit).all()
    return [_row(r) for r in rows]
