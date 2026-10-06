"""Center Admin: manage a centre's logins and their per-page permissions.

* Super Admin manages every centre-side login.
* A Center Admin (user_center_access.is_admin) manages logins for the centres
  they administer: create a login, link an existing centre login, set page
  permissions (reports / invoices / templates / center_info: none|read|write),
  make someone admin, unlink.
* Name / password of a login can only be changed by someone who administers
  every centre that login is linked to (so one centre cannot take over a
  login another centre also uses). Super Admin can always.
* A centre always keeps at least one admin (a Center Admin cannot remove the
  last one).
"""
import re
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.database import get_db
    from app.models import CenterDB, UserDB, UserCenterAccessDB
    from app.security import get_current_user, hash_password
    from app import access as access_svc
except ImportError:
    from app.database import get_db
    from app.models import CenterDB, UserDB, UserCenterAccessDB
    from app.security import get_current_user, hash_password
    from app import access as access_svc

router = APIRouter(prefix="/center-users", tags=["Center Users"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CenterLink(BaseModel):
    centerId: str
    isAdmin: bool = False
    permissions: Dict[str, str] = {}


class CenterUserCreate(BaseModel):
    name: Optional[str] = None
    email: str
    password: Optional[str] = None
    centers: List[CenterLink]


class CenterUserUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    centers: List[CenterLink]


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _manageable(db: Session, user: UserDB) -> Optional[List[str]]:
    """Centres the caller may manage (None = all). 403 when none."""
    if user.role == "SUPER_ADMIN":
        return None
    if user.role != "CENTER":
        raise HTTPException(status_code=403, detail="Only Super Admin and Center Admins manage centre users")
    ids = access_svc.admin_center_ids(db, user) or []
    if not ids:
        raise HTTPException(status_code=403, detail="You are not a Center Admin")
    return ids


def _links_of(db: Session, u: UserDB) -> List[dict]:
    rows = db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == u.id).all()
    if rows:
        return [{"centerId": r.center_id, "isAdmin": bool(r.is_admin),
                 "permissions": access_svc.normalize_permissions(r.permissions)} for r in rows]
    cid = (u.metadata_ or {}).get("centerId")
    if cid:  # not yet backfilled: primary centre, admin, full rights
        return [{"centerId": cid, "isAdmin": True, "permissions": dict(access_svc.FULL_PERMISSIONS)}]
    return []


def _materialize_fallback(db: Session, u: UserDB) -> None:
    """Turn the implicit primary-centre access into a real row before editing links."""
    if db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == u.id).first():
        return
    cid = (u.metadata_ or {}).get("centerId")
    if cid:
        db.add(UserCenterAccessDB(user_id=u.id, center_id=cid, is_admin=True,
                                  permissions=dict(access_svc.FULL_PERMISSIONS), created_at=_now(),
                                  created_by="materialize"))
        db.flush()


def _center_names(db: Session, ids) -> Dict[str, str]:
    ids = list(ids)
    if not ids:
        return {}
    return {c.id: c.center_name for c in db.query(CenterDB).filter(CenterDB.id.in_(ids)).all()}


def _user_out(db: Session, u: UserDB, visible: Optional[List[str]]) -> dict:
    links = _links_of(db, u)
    all_ids = [l["centerId"] for l in links]
    shown = links if visible is None else [l for l in links if l["centerId"] in visible]
    names = _center_names(db, [l["centerId"] for l in shown])
    for l in shown:
        l["centerName"] = names.get(l["centerId"], l["centerId"])
    shown.sort(key=lambda l: (l["centerName"] or "").lower())
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "primaryCenterId": (u.metadata_ or {}).get("centerId"),
        "centers": shown,
        "otherCenterCount": len([c for c in all_ids if visible is not None and c not in visible]),
        "canEditLogin": visible is None or all(c in visible for c in all_ids),
    }


def _check_links(db: Session, links: List[CenterLink], manageable: Optional[List[str]]) -> List[CenterLink]:
    seen = set()
    out = []
    existing = {c.id for c in db.query(CenterDB.id).all()}
    for l in links:
        cid = (l.centerId or "").strip()
        if not cid or cid in seen:
            continue
        if cid not in existing:
            raise HTTPException(status_code=400, detail=f"Unknown centre {cid}")
        if manageable is not None and cid not in manageable:
            raise HTTPException(status_code=403, detail="You can only grant access to centres you administer")
        seen.add(cid)
        out.append(CenterLink(centerId=cid, isAdmin=bool(l.isAdmin),
                              permissions=access_svc.normalize_permissions(l.permissions)))
    return out


def _admin_count(db: Session, center_id: str) -> int:
    n = db.query(UserCenterAccessDB).filter(
        UserCenterAccessDB.center_id == center_id, UserCenterAccessDB.is_admin.is_(True)).count()
    linked = {r[0] for r in db.query(UserCenterAccessDB.user_id).distinct().all()}
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if u.id not in linked and (u.metadata_ or {}).get("centerId") == center_id:
            n += 1
    return n


def _validate_password(pw: Optional[str]) -> None:
    if pw is not None and pw != "" and len(pw) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")


@router.get("")
def list_center_users(db: Session = Depends(get_db), current_user: UserDB = Depends(get_current_user)):
    manageable = _manageable(db, current_user)
    users = db.query(UserDB).filter(UserDB.role == "CENTER").order_by(UserDB.name.asc()).all()
    out = []
    for u in users:
        links = _links_of(db, u)
        if manageable is not None and not any(l["centerId"] in manageable for l in links):
            continue
        out.append(_user_out(db, u, manageable))
    centers_q = db.query(CenterDB)
    if manageable is not None:
        centers_q = centers_q.filter(CenterDB.id.in_(manageable))
    centers = sorted(({"centerId": c.id, "centerName": c.center_name} for c in centers_q.all()),
                     key=lambda c: (c["centerName"] or "").lower())
    return {"users": out, "manageableCenters": centers, "pages": list(access_svc.PAGES),
            "currentUserId": current_user.id}


@router.post("")
def create_center_user(body: CenterUserCreate, db: Session = Depends(get_db),
                       current_user: UserDB = Depends(get_current_user)):
    """Create a centre login, or link an existing centre login (same email) to your centres."""
    manageable = _manageable(db, current_user)
    email = (body.email or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    links = _check_links(db, body.centers, manageable)
    if not links:
        raise HTTPException(status_code=400, detail="Pick at least one centre")
    existing = db.query(UserDB).filter(UserDB.email.ilike(email)).first()
    if existing is not None:
        if existing.role != "CENTER":
            raise HTTPException(status_code=409, detail="This email already belongs to a non-centre account")
        _materialize_fallback(db, existing)
        already = {r.center_id for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == existing.id).all()}
        added = 0
        for l in links:
            if l.centerId in already:
                continue
            db.add(UserCenterAccessDB(user_id=existing.id, center_id=l.centerId, is_admin=l.isAdmin,
                                      permissions=l.permissions, created_at=_now(), created_by=current_user.email))
            added += 1
        if not added:
            raise HTTPException(status_code=409, detail="This login is already linked to the selected centre(s)")
        db.flush()
        access_svc.reassign_primary_center(db, existing)
        db.commit()
        return {"linkedExisting": True, "user": _user_out(db, existing, manageable)}

    name = " ".join((body.name or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not body.password:
        raise HTTPException(status_code=400, detail="Password is required for a new login")
    _validate_password(body.password)
    u = UserDB(email=email, name=name, role="CENTER",
               metadata_={"password": hash_password(body.password), "centerId": links[0].centerId})
    db.add(u)
    db.flush()
    for l in links:
        db.add(UserCenterAccessDB(user_id=u.id, center_id=l.centerId, is_admin=l.isAdmin,
                                  permissions=l.permissions, created_at=_now(), created_by=current_user.email))
    db.commit()
    db.refresh(u)
    return {"linkedExisting": False, "user": _user_out(db, u, manageable)}


@router.put("/{user_id}")
def update_center_user(user_id: int, body: CenterUserUpdate, db: Session = Depends(get_db),
                       current_user: UserDB = Depends(get_current_user)):
    """Replace this login's links for the centres you manage (others are left alone);
    optionally rename / reset the password."""
    manageable = _manageable(db, current_user)
    u = db.query(UserDB).filter(UserDB.id == user_id, UserDB.role == "CENTER").first()
    if not u:
        raise HTTPException(status_code=404, detail="Centre user not found")
    current_links = _links_of(db, u)
    if manageable is not None and not any(l["centerId"] in manageable for l in current_links):
        raise HTTPException(status_code=404, detail="Centre user not found")
    links = _check_links(db, body.centers, manageable)

    wants_login_change = bool((body.name or "").strip() and body.name.strip() != u.name) or bool(body.password)
    if wants_login_change and manageable is not None:
        if not all(l["centerId"] in manageable for l in current_links):
            raise HTTPException(status_code=403,
                                detail="This login is also used by another centre; only the Super Admin can rename it or reset its password")
    _validate_password(body.password)

    _materialize_fallback(db, u)
    scope = set(manageable) if manageable is not None else None
    rows = db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == u.id).all()
    by_center = {r.center_id: r for r in rows}
    wanted = {l.centerId: l for l in links}
    touched = set()
    for cid, r in by_center.items():
        if scope is not None and cid not in scope:
            continue
        if cid not in wanted:
            db.delete(r)
            touched.add(cid)
    for cid, l in wanted.items():
        r = by_center.get(cid)
        if r is None:
            db.add(UserCenterAccessDB(user_id=u.id, center_id=cid, is_admin=l.isAdmin,
                                      permissions=l.permissions, created_at=_now(), created_by=current_user.email))
        else:
            r.is_admin = l.isAdmin
            r.permissions = l.permissions
            flag_modified(r, "permissions")
        touched.add(cid)
    db.flush()
    if current_user.role != "SUPER_ADMIN":
        for cid in touched:
            if _admin_count(db, cid) < 1:
                db.rollback()
                raise HTTPException(status_code=400, detail="A centre must keep at least one Center Admin")
    if (body.name or "").strip():
        u.name = " ".join(body.name.split())
    if body.password:
        meta = dict(u.metadata_ or {})
        meta["password"] = hash_password(body.password)
        u.metadata_ = meta
        flag_modified(u, "metadata_")
    access_svc.reassign_primary_center(db, u)
    db.commit()
    db.refresh(u)
    return {"user": _user_out(db, u, manageable)}


@router.delete("/{user_id}/centers/{center_id}")
def unlink_center_user(user_id: int, center_id: str, db: Session = Depends(get_db),
                       current_user: UserDB = Depends(get_current_user)):
    """Remove one login's access to one centre (the login itself is not deleted)."""
    manageable = _manageable(db, current_user)
    if manageable is not None and center_id not in manageable:
        raise HTTPException(status_code=403, detail="You can only manage centres you administer")
    u = db.query(UserDB).filter(UserDB.id == user_id, UserDB.role == "CENTER").first()
    if not u:
        raise HTTPException(status_code=404, detail="Centre user not found")
    _materialize_fallback(db, u)
    row = db.query(UserCenterAccessDB).filter(
        UserCenterAccessDB.user_id == u.id, UserCenterAccessDB.center_id == center_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="This login is not linked to that centre")
    db.delete(row)
    db.flush()
    if current_user.role != "SUPER_ADMIN" and _admin_count(db, center_id) < 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="A centre must keep at least one Center Admin")
    access_svc.reassign_primary_center(db, u)
    db.commit()
    return {"ok": True}
