"""Soft-delete helpers: deleted_at timestamp columns + active-row filters.

Patient cases, doctors, centres and login users are marked deleted instead of
removed so history (invoices, studies, etc.) stays intact. List/login paths
must use the helpers below so soft-deleted rows stay hidden.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, Query

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def ensure_soft_delete_columns(bind) -> None:
    """ADD deleted_at to cases / doctors / centres / users (Postgres + SQLite)."""
    specs = [
        ("cases", "deleted_at", "VARCHAR(50)"),
        ("doctors", "deleted_at", "VARCHAR(50)"),
        ("radiology_centers", "deleted_at", "VARCHAR(50)"),
        ("users", "deleted_at", "VARCHAR(50)"),
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
                        # Column already exists on SQLite, or table missing in a fresh env
                        logger.debug("ensure_soft_delete_columns %s.%s: %s", table, col, e)
        logger.info("Ensured soft-delete columns (deleted_at) on cases/doctors/centers/users")
        # Hide leftover logins from centres that were hard-deleted before soft-delete existed
        try:
            from sqlalchemy.orm import Session as _S
            with _S(bind=bind) as db:
                n = cleanup_orphan_center_logins(db)
                if n:
                    logger.info("Soft-deleted %s orphan centre login(s)", n)
        except Exception as e:
            logger.warning("orphan centre login cleanup skipped: %s", e)
    except Exception as e:
        logger.warning("ensure_soft_delete_columns skipped: %s", e)


def cleanup_orphan_center_logins(db: Session) -> int:
    """Soft-delete CENTER users that have no remaining non-deleted centre link."""
    try:
        from app.models import CenterDB, UserDB, UserCenterAccessDB
    except ImportError:
        from app.models import CenterDB, UserDB, UserCenterAccessDB

    active_ids = {c.id for c in active_filter(db.query(CenterDB), CenterDB).all()}
    n = 0
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if is_deleted(u):
            continue
        link_ids = [
            r.center_id
            for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == u.id).all()
        ]
        if not link_ids:
            cid = (u.metadata_ or {}).get("centerId")
            if cid:
                link_ids = [str(cid)]
        if not any(cid in active_ids for cid in link_ids):
            mark_deleted(u)
            n += 1
    if n:
        db.commit()
    return n


def is_deleted(row) -> bool:
    return bool(getattr(row, "deleted_at", None))


def mark_deleted(row, when: Optional[str] = None) -> None:
    if row is None:
        return
    if getattr(row, "deleted_at", None):
        return
    row.deleted_at = when or now_iso()


def clear_deleted(row) -> None:
    if row is None:
        return
    row.deleted_at = None


def active_filter(query: Query, model) -> Query:
    """Exclude soft-deleted rows. Safe when deleted_at column is present."""
    col = getattr(model, "deleted_at", None)
    if col is None:
        return query
    return query.filter(col.is_(None))


def soft_delete_user(db: Session, user) -> None:
    mark_deleted(user)


def soft_delete_doctor_and_login(db: Session, doctor_id: str) -> None:
    try:
        from app.models import DoctorDB, UserDB
    except ImportError:
        from app.models import DoctorDB, UserDB

    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if doc and not is_deleted(doc):
        mark_deleted(doc)
    # Linked doctor login (metadata.doctorId)
    for u in db.query(UserDB).filter(UserDB.role == "DOCTOR").all():
        if is_deleted(u):
            continue
        meta = u.metadata_ or {}
        if str(meta.get("doctorId") or "") == str(doctor_id):
            mark_deleted(u)


def soft_delete_center_and_orphaned_logins(db: Session, center_id: str) -> None:
    """Soft-delete the centre, drop access links, soft-delete logins left with no live centre.

    Pricing rows are left in place (no hard delete) under the soft-deleted centre.
    """
    try:
        from app.models import CenterDB, UserDB, UserCenterAccessDB
        from app import access as access_svc
    except ImportError:
        from app.models import CenterDB, UserDB, UserCenterAccessDB
        from app import access as access_svc

    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if center and not is_deleted(center):
        mark_deleted(center)

    access_svc.unlink_center_everywhere(db, center_id)

    # Soft-delete CENTER logins that no longer have any non-deleted centre
    active_center_ids = {
        c.id for c in active_filter(db.query(CenterDB), CenterDB).all()
    }
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if is_deleted(u):
            continue
        link_ids = [
            r.center_id
            for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == u.id).all()
        ]
        if not link_ids:
            cid = (u.metadata_ or {}).get("centerId")
            if cid:
                link_ids = [str(cid)]
        if not any(cid in active_center_ids for cid in link_ids):
            mark_deleted(u)


def soft_delete_case(db: Session, case_id: str) -> dict:
    """Mark the patient case deleted. Studies/files kept for history; lists hide it."""
    try:
        from app.models import CaseDB, ReportDB
    except ImportError:
        from app.models import CaseDB, ReportDB

    counts = {"cases": 0, "xray_reports": 0}
    case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    if case and not is_deleted(case):
        mark_deleted(case)
        counts["cases"] = 1
    legacy = db.query(ReportDB).filter(ReportDB.id == case_id).first()
    if legacy is not None and hasattr(legacy, "deleted_at"):
        # legacy table may not have the column; ignore if missing
        pass
    return counts


def user_may_login(db: Session, user) -> bool:
    """False when the account is soft-deleted or frozen, or its only centres / doctor are."""
    if user is None or is_deleted(user):
        return False
    if getattr(user, "frozen_at", None):
        return False
    try:
        from app.models import CenterDB, DoctorDB, UserCenterAccessDB
        from app.entity_lifecycle import center_is_usable
    except ImportError:
        from app.models import CenterDB, DoctorDB, UserCenterAccessDB
        from app.entity_lifecycle import center_is_usable

    if user.role == "CENTER":
        link_ids = [
            r.center_id
            for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == user.id).all()
        ]
        if not link_ids:
            cid = (user.metadata_ or {}).get("centerId")
            if cid:
                link_ids = [str(cid)]
        if not link_ids:
            return False
        centers = db.query(CenterDB).filter(CenterDB.id.in_(link_ids)).all()
        return any(center_is_usable(c) for c in centers)

    if user.role == "DOCTOR":
        doc_id = (user.metadata_ or {}).get("doctorId")
        if not doc_id:
            return True
        doc = db.query(DoctorDB).filter(DoctorDB.id == str(doc_id)).first()
        if doc is None:
            return True  # login without a doctor profile row still allowed
        if is_deleted(doc):
            return False
        if getattr(doc, "frozen_at", None):
            return False
        return True

    return True
