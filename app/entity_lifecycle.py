"""Freeze / unfreeze + durable audit log for doctors, centres, and centre users."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text as sa_text, inspect
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def ensure_freeze_and_audit_tables(bind) -> None:
    """ADD frozen_at columns + create audit_logs if missing (Postgres + SQLite)."""
    specs = [
        ("doctors", "frozen_at", "VARCHAR(50)"),
        ("radiology_centers", "frozen_at", "VARCHAR(50)"),
        ("users", "frozen_at", "VARCHAR(50)"),
    ]
    try:
        with bind.begin() as conn:
            for table, col, ddl in specs:
                try:
                    conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
                except Exception:
                    try:
                        conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    except Exception as e:
                        logger.debug("ensure frozen_at %s.%s: %s", table, col, e)
        try:
            from app.models import AuditLogDB
            from app.database import Base
        except ImportError:
            from app.models import AuditLogDB
            from app.database import Base
        insp = inspect(bind)
        if "audit_logs" not in insp.get_table_names():
            Base.metadata.create_all(bind=bind, tables=[AuditLogDB.__table__])
            logger.info("Created audit_logs table")
        logger.info("Ensured frozen_at columns + audit_logs")
    except Exception as e:
        logger.warning("ensure_freeze_and_audit_tables skipped: %s", e)


def is_frozen(row) -> bool:
    return bool(getattr(row, "frozen_at", None))


def mark_frozen(row, when: Optional[str] = None) -> None:
    if row is None:
        return
    row.frozen_at = when or now_iso()


def clear_frozen(row) -> None:
    if row is None:
        return
    row.frozen_at = None


def center_is_usable(center) -> bool:
    """Centre can be used for login/upload when not soft-deleted and not frozen."""
    if center is None:
        return False
    if getattr(center, "deleted_at", None):
        return False
    if getattr(center, "frozen_at", None):
        return False
    return True


def write_audit(
    db: Session,
    *,
    actor,
    entity_type: str,
    entity_id: str,
    action: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    approval_id: Optional[str] = None,
    note: Optional[str] = None,
    commit: bool = False,
) -> Any:
    try:
        from app.models import AuditLogDB
    except ImportError:
        from app.models import AuditLogDB

    row = AuditLogDB(
        id=f"aud-{uuid.uuid4().hex[:22]}",
        at=now_iso(),
        actor_user_id=getattr(actor, "id", None) if actor is not None else None,
        actor_name=(getattr(actor, "name", None) or getattr(actor, "email", None) or "system") if actor else "system",
        actor_role=getattr(actor, "role", None) if actor else None,
        entity_type=entity_type,
        entity_id=str(entity_id),
        action=action.upper(),
        before=before,
        after=after,
        approval_id=approval_id,
        note=note,
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def snapshot_doctor_row(d) -> Optional[dict]:
    if d is None:
        return None
    meta = d.metadata_ or {}
    return {
        "id": d.id,
        "fullName": d.full_name,
        "email": d.email,
        "username": d.username,
        "contactNumber": d.contact_number,
        "deletedAt": getattr(d, "deleted_at", None),
        "frozenAt": getattr(d, "frozen_at", None),
        "degree": meta.get("degree"),
        "registrationNumber": meta.get("registrationNumber"),
    }


def snapshot_center_row(c) -> Optional[dict]:
    if c is None:
        return None
    meta = c.metadata_ or {}
    return {
        "id": c.id,
        "centerName": c.center_name,
        "email": c.email,
        "contactNumber": c.contact_number,
        "username": meta.get("username"),
        "deletedAt": getattr(c, "deleted_at", None),
        "frozenAt": getattr(c, "frozen_at", None),
    }


def snapshot_center_user_row(u) -> Optional[dict]:
    if u is None:
        return None
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "deletedAt": getattr(u, "deleted_at", None),
        "frozenAt": getattr(u, "frozen_at", None),
        "primaryCenterId": (u.metadata_ or {}).get("centerId"),
    }


def set_doctor_frozen(db: Session, doctor_id: str, frozen: bool, actor=None, approval_id: Optional[str] = None) -> dict:
    try:
        from app.models import DoctorDB
        from app.soft_delete import is_deleted
    except ImportError:
        from app.models import DoctorDB
        from app.soft_delete import is_deleted

    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if not doc or is_deleted(doc):
        raise LookupError("Doctor not found")
    before = snapshot_doctor_row(doc)
    if frozen:
        mark_frozen(doc)
        action = "FREEZE"
    else:
        clear_frozen(doc)
        action = "UNFREEZE"
    after = snapshot_doctor_row(doc)
    write_audit(
        db, actor=actor, entity_type="doctor", entity_id=doctor_id, action=action,
        before=before, after=after, approval_id=approval_id,
    )
    db.commit()
    return after


def set_center_frozen(db: Session, center_id: str, frozen: bool, actor=None, approval_id: Optional[str] = None) -> dict:
    try:
        from app.models import CenterDB
        from app.soft_delete import is_deleted
    except ImportError:
        from app.models import CenterDB
        from app.soft_delete import is_deleted

    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not center or is_deleted(center):
        raise LookupError("Centre not found")
    before = snapshot_center_row(center)
    if frozen:
        mark_frozen(center)
        action = "FREEZE"
    else:
        clear_frozen(center)
        action = "UNFREEZE"
    after = snapshot_center_row(center)
    write_audit(
        db, actor=actor, entity_type="center", entity_id=center_id, action=action,
        before=before, after=after, approval_id=approval_id,
    )
    db.commit()
    return after


def set_center_user_frozen(db: Session, user_id: int, frozen: bool, actor=None, approval_id: Optional[str] = None) -> dict:
    try:
        from app.models import UserDB
        from app.soft_delete import is_deleted
    except ImportError:
        from app.models import UserDB
        from app.soft_delete import is_deleted

    u = db.query(UserDB).filter(UserDB.id == user_id, UserDB.role == "CENTER").first()
    if not u or is_deleted(u):
        raise LookupError("Centre user not found")
    before = snapshot_center_user_row(u)
    if frozen:
        mark_frozen(u)
        action = "FREEZE"
    else:
        clear_frozen(u)
        action = "UNFREEZE"
    after = snapshot_center_user_row(u)
    write_audit(
        db, actor=actor, entity_type="center_user", entity_id=str(user_id), action=action,
        before=before, after=after, approval_id=approval_id,
    )
    db.commit()
    return after
