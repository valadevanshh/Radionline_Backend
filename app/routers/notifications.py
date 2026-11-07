"""Bell notifications for the logged-in user (see backend/app/notifications.py)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

try:
    from app.database import get_db
    from app.models import UserDB, UserNotificationDB
    from app.security import get_current_user
    from app.notifications import to_schema
except ImportError:
    from app.database import get_db
    from app.models import UserDB, UserNotificationDB
    from app.security import get_current_user
    from app.notifications import to_schema

router = APIRouter(prefix="/notifications", tags=["Notifications"])

LIST_LIMIT = 50


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


@router.get("")
def list_my_notifications(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    q = db.query(UserNotificationDB).filter(UserNotificationDB.user_id == current_user.id)
    if current_user.role == "DOCTOR":
        # Over-fetch so deleted-patient notices don't crowd out other recent items.
        rows = q.order_by(UserNotificationDB.id.desc()).limit(LIST_LIMIT * 4).all()
        try:
            from app.soft_delete import hidden_patient_case_ids
        except ImportError:
            from app.soft_delete import hidden_patient_case_ids
        hidden = hidden_patient_case_ids(db, [r.case_id for r in rows if r.case_id])
        rows = [r for r in rows if not r.case_id or r.case_id not in hidden]
        unread = sum(1 for r in rows if not r.read_at)
        rows = rows[:LIST_LIMIT]
    else:
        rows = q.order_by(UserNotificationDB.id.desc()).limit(LIST_LIMIT).all()
        unread = q.filter(UserNotificationDB.read_at.is_(None)).count()
    return {"items": [to_schema(r) for r in rows], "unread": unread}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    n = (
        db.query(UserNotificationDB)
        .filter(UserNotificationDB.user_id == current_user.id, UserNotificationDB.read_at.is_(None))
        .update({"read_at": _now_iso()}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "updated": n}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    row = db.query(UserNotificationDB).filter(UserNotificationDB.id == notification_id).first()
    # Someone else's notification answers 404 too, so ids cannot be probed
    if not row or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not row.read_at:
        row.read_at = _now_iso()
        db.commit()
        db.refresh(row)
    return to_schema(row)
