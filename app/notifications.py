"""Per-user bell notifications: persisted rows + targeted live push over the WebSocket.

Used for:
  * CASE_REASSIGNED -> the doctor a case was reassigned to
  * CASE_MESSAGE    -> a new Case Activity comment: the case's center user(s),
                       every Super Admin and Manager, and the assigned doctor(s)
The sender is never notified. Notification failures never break the action that
triggered them (they are logged and skipped).
"""
import logging
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

try:
    from app.models import Base, UserDB, DoctorDB, CaseDB, UserNotificationDB
    from app.websocket import manager
except ImportError:
    from app.models import Base, UserDB, DoctorDB, CaseDB, UserNotificationDB
    from app.websocket import manager

logger = logging.getLogger(__name__)

KIND_CASE_REASSIGNED = "CASE_REASSIGNED"
KIND_CASE_MESSAGE = "CASE_MESSAGE"
KIND_APPROVAL_REQUEST = "APPROVAL_REQUEST"   # -> every Super Admin: a Manager asked to edit / delete
KIND_APPROVAL_RESULT = "APPROVAL_RESULT"     # -> the Manager: approved / rejected
WS_EVENT = "USER_NOTIFICATION"


def ensure_user_notification_tables(bind) -> None:
    """Create user_notifications if missing (Postgres + SQLite). Additive only."""
    Base.metadata.create_all(bind=bind, tables=[UserNotificationDB.__table__])
    logger.info("Ensured table exists: user_notifications")


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clip(text: Optional[str], limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 3].rstrip() + "..."


def case_label(case: CaseDB) -> str:
    name = (case.full_name or "").strip() or "Patient"
    num = (case.patient_number or "").strip()
    return f"{name} ({num})" if num else name


def case_link(case_id: str, activity: bool = False) -> str:
    return f"/dashboard/workspace/{case_id}" + ("?activity=1" if activity else "")


def to_schema(row: UserNotificationDB) -> dict:
    return {
        "id": row.id,
        "userId": row.user_id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "caseId": row.case_id,
        "link": row.link,
        "actorUserId": row.actor_user_id,
        "actorName": row.actor_name,
        "createdAt": row.created_at,
        "readAt": row.read_at,
    }


# ---------------------------------------------------------------- recipients

def doctor_user_ids(db: Session, doctor_refs: Iterable[str]) -> List[int]:
    """User ids of DOCTOR accounts matching a doctor id / email / username."""
    refs = {str(r).strip() for r in doctor_refs if r and str(r).strip()}
    if not refs:
        return []
    docs = {d.id: d for d in db.query(DoctorDB).all()}
    out: List[int] = []
    for u in db.query(UserDB).filter(UserDB.role == "DOCTOR").all():
        doctor_id = str((u.metadata_ or {}).get("doctorId") or "")
        keys = {doctor_id, u.email or ""}
        doc = docs.get(doctor_id)
        if doc is not None:
            keys.update({doc.email or "", doc.username or ""})
        keys.discard("")
        if keys & refs:
            out.append(u.id)
    return out


def center_user_ids(db: Session, center_id: Optional[str]) -> List[int]:
    """Centre-side accounts that can see this centre's reports (multi-centre links included)."""
    if not center_id:
        return []
    try:
        from app import access
    except ImportError:
        from app import access
    try:
        return access.center_user_ids(db, center_id, "reports", "read")
    except Exception as e:  # table missing etc.: fall back to the primary centre only
        logger.warning(f"center_user_ids via access links failed: {e}")
        return [
            u.id
            for u in db.query(UserDB).filter(UserDB.role == "CENTER").all()
            if (u.metadata_ or {}).get("centerId") == center_id
        ]


def admin_user_ids(db: Session) -> List[int]:
    return [u.id for u in db.query(UserDB).filter(UserDB.role.in_(("SUPER_ADMIN", "MANAGER"))).all()]


def super_admin_user_ids(db: Session) -> List[int]:
    return [u.id for u in db.query(UserDB).filter(UserDB.role == "SUPER_ADMIN").all()]


# ---------------------------------------------------------------- create + push

def create_for_users(
    db: Session,
    user_ids: Iterable[int],
    *,
    kind: str,
    title: str,
    body: Optional[str],
    case_id: Optional[str],
    link: Optional[str],
    actor: Optional[UserDB],
) -> List[dict]:
    """Persist one notification per recipient (deduplicated, sender excluded) and commit."""
    actor_id = actor.id if actor is not None else None
    seen = set()
    rows: List[UserNotificationDB] = []
    when = _now_iso()
    for uid in user_ids:
        if uid is None or uid in seen or (actor_id is not None and uid == actor_id):
            continue
        seen.add(uid)
        row = UserNotificationDB(
            user_id=uid,
            kind=kind,
            title=_clip(title, 255),
            body=_clip(body, 500) if body else None,
            case_id=case_id,
            link=link,
            actor_user_id=actor_id,
            actor_name=actor.name if actor is not None else None,
            created_at=when,
            read_at=None,
        )
        db.add(row)
        rows.append(row)
    if not rows:
        return []
    db.commit()
    return [to_schema(r) for r in rows]


async def push(items: List[dict]) -> None:
    """Live delivery: each recipient's sockets receive only their own notification."""
    for item in items:
        try:
            await manager.send_to_users([item["userId"]], {"type": WS_EVENT, "notification": item})
        except Exception as e:  # never break the triggering action
            logger.warning(f"Notification push failed: {e}")


async def notify(db: Session, user_ids: Iterable[int], **kwargs) -> List[dict]:
    """create_for_users + push; logs and swallows errors (caller's action already committed)."""
    try:
        items = create_for_users(db, user_ids, **kwargs)
    except Exception as e:
        logger.warning(f"Notification create failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []
    await push(items)
    return items


ROLE_LABELS = {"SUPER_ADMIN": "Super Admin", "MANAGER": "Manager", "CENTER": "Center", "DOCTOR": "Doctor"}


async def notify_case_reassigned(
    db: Session, case: CaseDB, to_doctor_id: str, actor: Optional[UserDB], reason: Optional[str] = None
) -> List[dict]:
    by = f" by {actor.name}" if actor is not None and actor.name else ""
    body = f"{case_label(case)} was reassigned to you{by}."
    if reason and str(reason).strip():
        body += f" Reason: {_clip(reason, 200)}"
    return await notify(
        db,
        doctor_user_ids(db, [to_doctor_id]),
        kind=KIND_CASE_REASSIGNED,
        title=f"Case reassigned to you: {case_label(case)}",
        body=body,
        case_id=case.id,
        link=case_link(case.id),
        actor=actor,
    )


async def notify_case_message(
    db: Session, case: CaseDB, doctor_refs: Iterable[str], actor: UserDB, text: str
) -> List[dict]:
    recipients: List[int] = []
    recipients += center_user_ids(db, case.radiology_center_id)
    recipients += admin_user_ids(db)
    recipients += doctor_user_ids(db, doctor_refs)
    who = actor.name or "Someone"
    role = ROLE_LABELS.get(actor.role or "", actor.role or "")
    return await notify(
        db,
        recipients,
        kind=KIND_CASE_MESSAGE,
        title=f"New message: {case_label(case)}",
        body=f"{who} ({role}): {_clip(text, 160)}" if role else f"{who}: {_clip(text, 160)}",
        case_id=case.id,
        link=case_link(case.id, activity=True),
        actor=actor,
    )


# ---------------------------------------------------------------- approvals

_ENTITY_LABELS = {"case": "patient record", "doctor": "doctor", "center": "centre", "template": "template"}


def approval_label(action_type: Optional[str], entity_type: Optional[str], payload: Optional[dict],
                   entity_id: Optional[str] = None) -> str:
    """e.g. "Delete patient record RAMESH (P-1)", "Edit doctor Dr. A"."""
    at = (action_type or "").upper()
    verb = "Delete" if at.startswith("DELETE") else "Edit" if at.startswith("UPDATE") else "Create" if at.startswith("CREATE") else at.title()
    et = (entity_type or "").lower()
    if not et:
        for k in _ENTITY_LABELS:
            if k.upper() in at:
                et = k
                break
    p = payload or {}
    name = (
        p.get("fullName") or p.get("centerName") or p.get("title") or p.get("name") or entity_id or ""
    )
    num = p.get("patientNumber") if et == "case" else None
    label = f"{verb} {_ENTITY_LABELS.get(et, et or 'item')}"
    if name:
        label += f" {_clip(name, 80)}"
    if num:
        label += f" ({_clip(num, 40)})"
    return label


async def notify_approval_requested(db: Session, approval, actor: Optional[UserDB]) -> List[dict]:
    who = (actor.name if actor is not None and actor.name else None) or approval.manager_name or "A manager"
    what = approval_label(approval.action_type, approval.entity_type, approval.payload, approval.entity_id)
    return await notify(
        db,
        super_admin_user_ids(db),
        kind=KIND_APPROVAL_REQUEST,
        title=f"Approval needed: {what}",
        body=f"{who} (Manager) asked to {what[0].lower() + what[1:]}. Review it in Pending Approvals.",
        case_id=approval.entity_id if (approval.entity_type or "") == "case" else None,
        link="/dashboard/approvals",
        actor=actor,
    )


async def notify_approval_resolved(db: Session, approval, actor: Optional[UserDB]) -> List[dict]:
    manager_user = None
    if approval.manager_id:
        manager_user = db.query(UserDB).filter(UserDB.email.ilike(str(approval.manager_id))).first()
    if manager_user is None:
        return []
    what = approval_label(approval.action_type, approval.entity_type, approval.payload, approval.entity_id)
    ok = (approval.status or "").upper() == "APPROVED"
    body = f"Your request to {what[0].lower() + what[1:]} was {'approved' if ok else 'rejected'}"
    if actor is not None and actor.name:
        body += f" by {actor.name}"
    body += "."
    if not ok and approval.rejection_reason:
        body += f" Reason: {_clip(approval.rejection_reason, 200)}"
    return await notify(
        db,
        [manager_user.id],
        kind=KIND_APPROVAL_RESULT,
        title=f"Request {'approved' if ok else 'rejected'}: {what}",
        body=body,
        case_id=None,
        link="/dashboard/approvals",
        actor=actor,
    )
