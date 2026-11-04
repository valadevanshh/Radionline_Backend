"""Super Admin: Manager accounts and their per-page permissions.

GET    /api/managers              list managers (+ permissions, pending request count)
POST   /api/managers              create a manager {name, email, password, permissions}
PUT    /api/managers/{id}         update name / permissions / (optional) password
DELETE /api/managers/{id}         deactivate (soft delete) a manager login

Only the Super Admin can reach these routes; changes apply immediately.
"""
import logging
import re
import time
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models import PendingApprovalDB, UserDB
from app.security import hash_password, require_roles
from app.soft_delete import active_filter, is_deleted, soft_delete_user
from app.manager_access import manager_permissions, normalize_manager_permissions, MANAGER_PAGES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/managers", tags=["Managers"])

MIN_PW = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ManagerCreate(BaseModel):
    name: str
    email: str
    password: str
    permissions: Dict[str, Dict[str, bool]] = {}


class ManagerUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    permissions: Optional[Dict[str, Dict[str, bool]]] = None


def _pending_count(db: Session, email: str) -> int:
    return db.query(PendingApprovalDB).filter(
        PendingApprovalDB.manager_id == email, PendingApprovalDB.status == "PENDING"
    ).count()


def manager_out(db: Session, u: UserDB) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role,
        "permissions": manager_permissions(u),
        "pendingRequests": _pending_count(db, u.email),
        "createdAt": (u.metadata_ or {}).get("createdAt"),
    }


def _check_password(pw: str) -> None:
    if len(pw or "") < MIN_PW:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PW} characters")
    if len(pw) > 128:
        raise HTTPException(status_code=400, detail="Password is too long")


def _get_manager(db: Session, manager_id: int) -> UserDB:
    u = db.query(UserDB).filter(UserDB.id == manager_id, UserDB.role == "MANAGER").first()
    if not u or is_deleted(u):
        raise HTTPException(status_code=404, detail="Manager not found")
    return u


@router.get("")
def list_managers(db: Session = Depends(get_db), _sa: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    rows = active_filter(db.query(UserDB).filter(UserDB.role == "MANAGER"), UserDB).order_by(UserDB.id.asc()).all()
    return [manager_out(db, u) for u in rows]


@router.post("")
def create_manager(body: ManagerCreate, db: Session = Depends(get_db),
                   sa: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    name = " ".join((body.name or "").split())
    email = (body.email or "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email / login id")
    _check_password(body.password)
    existing = db.query(UserDB).filter(UserDB.email.ilike(email)).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A login with this email already exists")
    u = UserDB(
        email=email, name=name, role="MANAGER",
        metadata_={
            "password": hash_password(body.password),
            "managerPermissions": normalize_manager_permissions(body.permissions),
            "createdAt": int(time.time()),
            "createdBy": sa.email,
        },
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    logger.info("Manager created id=%s email=%s by %s", u.id, u.email, sa.email)
    return manager_out(db, u)


@router.put("/{manager_id}")
def update_manager(manager_id: int, body: ManagerUpdate, db: Session = Depends(get_db),
                   sa: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    u = _get_manager(db, manager_id)
    meta = dict(u.metadata_ or {})
    if body.name is not None:
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        u.name = name
    if body.permissions is not None:
        meta["managerPermissions"] = normalize_manager_permissions(body.permissions)
    if body.password:
        _check_password(body.password)
        meta["password"] = hash_password(body.password)
        meta["passwordChangedAt"] = int(time.time())  # ends the manager's old sessions
    u.metadata_ = meta
    flag_modified(u, "metadata_")
    db.commit()
    db.refresh(u)
    return manager_out(db, u)


@router.delete("/{manager_id}", status_code=204)
def delete_manager(manager_id: int, db: Session = Depends(get_db),
                   sa: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    u = _get_manager(db, manager_id)
    soft_delete_user(db, u)
    db.commit()
    return None
