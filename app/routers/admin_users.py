"""Super Admin: reset the password of any login (centre, doctor, manager, admin, centre user).

POST /api/admin/users/{user_ref}/reset-password   body: {"newPassword": "..."}
user_ref: numeric user id, an email, "doctor:<doctorId>" or "center:<centerId>".
The password is never returned or logged. Existing sessions of the target are ended
(tokens issued before the reset are rejected) unless the admin resets their own password.
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models import CenterDB, DoctorDB, UserDB
from app.security import hash_password, require_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["Admin Users"])

MIN_LEN = 8


class ResetPasswordIn(BaseModel):
    newPassword: str


def _by_email(db: Session, email: Optional[str]) -> Optional[UserDB]:
    email = (email or "").strip()
    return db.query(UserDB).filter(UserDB.email.ilike(email)).first() if email else None


def _with_meta(db: Session, role: str, key: str, value: str) -> Optional[UserDB]:
    for u in db.query(UserDB).filter(UserDB.role == role).order_by(UserDB.id.asc()).all():
        if (u.metadata_ or {}).get(key) == value:
            return u
    return None


def resolve_user(db: Session, user_ref: str) -> Optional[UserDB]:
    ref = (user_ref or "").strip()
    if ref.isdigit():
        return db.query(UserDB).filter(UserDB.id == int(ref)).first()
    kind, _, rid = ref.partition(":")
    if kind == "doctor" and rid:
        doc = db.query(DoctorDB).filter(DoctorDB.id == rid).first()
        if not doc:
            return None
        return _with_meta(db, "DOCTOR", "doctorId", doc.id) or _by_email(db, doc.username) or _by_email(db, doc.email)
    if kind == "center" and rid:
        c = db.query(CenterDB).filter(CenterDB.id == rid).first()
        if not c:
            return None
        u = _by_email(db, (c.metadata_ or {}).get("username")) or _by_email(db, c.email)
        return u if u is not None and u.role == "CENTER" else _with_meta(db, "CENTER", "centerId", c.id)
    if "@" in ref or kind not in ("doctor", "center"):
        return _by_email(db, ref)
    return None


def reset_password(db: Session, actor: UserDB, user_ref: str, new_password: str) -> dict:
    if actor.role != "SUPER_ADMIN":  # also enforced by the route dependency
        raise HTTPException(status_code=403, detail="Only the Super Admin can reset passwords")
    pw = new_password or ""
    if len(pw) < MIN_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_LEN} characters")
    if len(pw) > 128:
        raise HTTPException(status_code=400, detail="Password is too long")
    user = resolve_user(db, user_ref)
    if user is None:
        raise HTTPException(status_code=404, detail="No login found for this user")
    hashed = hash_password(pw)
    meta = dict(user.metadata_ or {})
    meta["password"] = hashed
    is_self = user.id == actor.id
    if not is_self:
        # get_current_user rejects tokens issued before this moment
        meta["passwordChangedAt"] = int(time.time())
    user.metadata_ = meta
    flag_modified(user, "metadata_")
    # Doctor / centre records keep a copy of the hash; keep it in step.
    for model, key in ((DoctorDB, "doctorId"), (CenterDB, "centerId")):
        rid = meta.get(key)
        if not rid:
            continue
        row = db.query(model).filter(model.id == rid).first()
        if row is not None and "password" in (row.metadata_ or {}):
            rm = dict(row.metadata_ or {})
            rm["password"] = hashed
            row.metadata_ = rm
            flag_modified(row, "metadata_")
    db.commit()
    logger.info("Password reset: user_id=%s email=%s role=%s by admin_id=%s (%s)%s",
                user.id, user.email, user.role, actor.id, actor.email, " [self]" if is_self else "")
    return {"ok": True, "userId": user.id, "email": user.email, "name": user.name, "role": user.role,
            "sessionsEnded": not is_self}


@router.post("/{user_ref}/reset-password")
def reset_password_route(
    user_ref: str,
    body: ResetPasswordIn,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    return reset_password(db, current_user, user_ref, body.newPassword)