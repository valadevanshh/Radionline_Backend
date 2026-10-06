"""Centre-side access control (multi-centre logins with per-page permissions).

Role CENTER accounts can be linked to several radiology centres through
user_center_access. Each link carries:
  * is_admin     -> Center Admin for that centre (may add / edit / unlink the
                    centre's users and their page permissions)
  * permissions  -> {"reports", "invoices", "templates", "center_info"}:
                    "none" | "read" | "write"

Super Admin and Manager are never restricted here (they see every centre).
Doctors are scoped by their own case rules elsewhere; centre pages do not
apply to them.

Compatibility: a CENTER account without any link row falls back to its
metadata.centerId as admin with full rights. The startup backfill turns that
into a real row, so the fallback only matters until the first restart.
"""
import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.models import Base, CenterDB, UserDB, UserCenterAccessDB, ImageAnnotationDB
    from app.security import user_public_dict
except ImportError:
    from app.models import Base, CenterDB, UserDB, UserCenterAccessDB, ImageAnnotationDB
    from app.security import user_public_dict

logger = logging.getLogger(__name__)

PAGES = ("reports", "invoices", "templates", "center_info")
LEVELS = {"none": 0, "read": 1, "write": 2}
FULL_PERMISSIONS = {p: "write" for p in PAGES}
UNRESTRICTED_ROLES = ("SUPER_ADMIN", "MANAGER")


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def normalize_permissions(raw) -> Dict[str, str]:
    """Every page present, unknown values -> "none"."""
    src = raw if isinstance(raw, dict) else {}
    out = {}
    for page in PAGES:
        val = str(src.get(page) or "none").strip().lower()
        out[page] = val if val in LEVELS else "none"
    return out


def _level_ok(have: str, need: str) -> bool:
    return LEVELS.get(have or "none", 0) >= LEVELS.get(need or "read", 1)


# ------------------------------------------------------------------ schema + backfill

def ensure_access_tables(bind) -> None:
    """Create user_center_access + image_annotations if missing, then backfill. Additive only."""
    Base.metadata.create_all(bind=bind, tables=[UserCenterAccessDB.__table__, ImageAnnotationDB.__table__])
    logger.info("Ensured tables exist: user_center_access, image_annotations")
    from sqlalchemy.orm import Session as _S
    with _S(bind=bind) as db:
        n = backfill_primary_center_admins(db)
        if n:
            logger.info("Backfilled %s centre admin link(s) from users.metadata.centerId", n)


def backfill_primary_center_admins(db: Session) -> int:
    """Every CENTER account with a metadata.centerId and no link rows at all becomes
    admin of that centre with full rights (idempotent)."""
    created = 0
    linked_users = {r[0] for r in db.query(UserCenterAccessDB.user_id).distinct().all()}
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if u.id in linked_users:
            continue
        cid = (u.metadata_ or {}).get("centerId")
        if not cid:
            continue
        db.add(UserCenterAccessDB(
            user_id=u.id, center_id=str(cid), is_admin=True,
            permissions=dict(FULL_PERMISSIONS), created_at=_now_iso(), created_by="backfill",
        ))
        created += 1
    if created:
        db.commit()
    return created


def ensure_primary_center_admin(db: Session, user: UserDB, center_id: str, created_by: Optional[str] = None) -> None:
    """Called when a centre's own login is created/updated: that login is admin of the centre."""
    if not user or not user.id or not center_id or user.role != "CENTER":
        return
    row = (
        db.query(UserCenterAccessDB)
        .filter(UserCenterAccessDB.user_id == user.id, UserCenterAccessDB.center_id == center_id)
        .first()
    )
    if row:
        if not row.is_admin:
            row.is_admin = True
        row.permissions = dict(FULL_PERMISSIONS)
        flag_modified(row, "permissions")
    else:
        db.add(UserCenterAccessDB(
            user_id=user.id, center_id=center_id, is_admin=True,
            permissions=dict(FULL_PERMISSIONS), created_at=_now_iso(), created_by=created_by,
        ))


# ------------------------------------------------------------------ lookups

def center_access_map(db: Session, user: UserDB) -> Optional[Dict[str, dict]]:
    """{center_id: {"isAdmin": bool, "permissions": {...}}} for CENTER accounts.
    None for roles that are not centre-scoped (Super Admin, Manager, Doctor)."""
    if user is None or user.role != "CENTER":
        return None
    rows = db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == user.id).all()
    if rows:
        return {
            r.center_id: {"isAdmin": bool(r.is_admin), "permissions": normalize_permissions(r.permissions)}
            for r in rows
        }
    cid = (user.metadata_ or {}).get("centerId")
    if cid:
        return {str(cid): {"isAdmin": True, "permissions": dict(FULL_PERMISSIONS)}}
    return {}


def has_center_perm(db: Session, user: UserDB, center_id: Optional[str], page: str, level: str = "read") -> bool:
    if user is None:
        return False
    if user.role in UNRESTRICTED_ROLES:
        return True
    if user.role != "CENTER" or not center_id:
        return False
    entry = (center_access_map(db, user) or {}).get(str(center_id))
    return bool(entry) and _level_ok(entry["permissions"].get(page, "none"), level)


def require_center_perm(db: Session, user: UserDB, center_id: Optional[str], page: str, level: str = "read") -> None:
    if not has_center_perm(db, user, center_id, page, level):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have {level} access to {page.replace('_', ' ')} for this centre",
        )


def center_ids_with(db: Session, user: UserDB, page: str, level: str = "read") -> Optional[List[str]]:
    """Centres where a CENTER account has at least `level` on `page`.
    None = not centre-restricted (Super Admin / Manager)."""
    if user.role in UNRESTRICTED_ROLES:
        return None
    if user.role != "CENTER":
        return []
    return [cid for cid, e in (center_access_map(db, user) or {}).items()
            if _level_ok(e["permissions"].get(page, "none"), level)]


def linked_center_ids(db: Session, user: UserDB) -> Optional[List[str]]:
    """Every centre a CENTER account is linked to (any permission). None = unrestricted."""
    if user.role in UNRESTRICTED_ROLES:
        return None
    if user.role != "CENTER":
        return []
    return list((center_access_map(db, user) or {}).keys())


def admin_center_ids(db: Session, user: UserDB) -> Optional[List[str]]:
    """Centres whose users this account may manage. None = all (Super Admin)."""
    if user.role == "SUPER_ADMIN":
        return None
    if user.role != "CENTER":
        return []
    return [cid for cid, e in (center_access_map(db, user) or {}).items() if e["isAdmin"]]


def center_user_ids(db: Session, center_id: Optional[str], page: str = "reports", level: str = "read") -> List[int]:
    """User ids of CENTER accounts with `level` on `page` for this centre (notifications)."""
    if not center_id:
        return []
    out: List[int] = []
    linked = set()
    for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.center_id == str(center_id)).all():
        linked.add(r.user_id)
        if _level_ok(normalize_permissions(r.permissions).get(page, "none"), level):
            out.append(r.user_id)
    users_with_rows = {r[0] for r in db.query(UserCenterAccessDB.user_id).distinct().all()}
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if u.id in users_with_rows:
            continue
        if (u.metadata_ or {}).get("centerId") == center_id:
            out.append(u.id)  # pre-backfill fallback: primary centre, full rights
    return out


def all_center_count(db: Session) -> int:
    return db.query(CenterDB).count()


def session_dict(db: Session, user: UserDB) -> dict:
    """user_public_dict + what the UI needs to scope itself:
    centers (CENTER only), centerCount, multiCenter, pagePermissions, canManageCenterUsers."""
    base = user_public_dict(user)
    base["userId"] = user.id
    amap = center_access_map(db, user)
    if amap is None:
        count = all_center_count(db)
        base.update({
            "centers": [],
            "centerCount": count,
            "multiCenter": count > 1,
            "pagePermissions": None,
            "canManageCenterUsers": user.role == "SUPER_ADMIN",
        })
        return base
    names = {}
    if amap:
        for c in db.query(CenterDB).filter(CenterDB.id.in_(list(amap.keys()))).all():
            names[c.id] = c.center_name
    centers = [
        {"centerId": cid, "centerName": names.get(cid) or cid, "isAdmin": e["isAdmin"], "permissions": e["permissions"]}
        for cid, e in amap.items()
    ]
    centers.sort(key=lambda c: (c["centerName"] or "").lower())
    page_perms = {}
    for page in PAGES:
        best = "none"
        for e in amap.values():
            lv = e["permissions"].get(page, "none")
            if LEVELS[lv] > LEVELS[best]:
                best = lv
        page_perms[page] = best
    primary = base.get("centerId")
    if (not primary or primary not in amap) and centers:
        base["centerId"] = centers[0]["centerId"]
    base.update({
        "centers": centers,
        "centerCount": len(centers),
        "multiCenter": len(centers) > 1,
        "pagePermissions": page_perms,
        "canManageCenterUsers": any(e["isAdmin"] for e in amap.values()),
    })
    return base


def reassign_primary_center(db: Session, user: UserDB) -> None:
    """Keep metadata.centerId pointing at a centre the account is still linked to (or drop it)."""
    if user.role != "CENTER":
        return
    ids = [r.center_id for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.user_id == user.id).all()]
    meta = dict(user.metadata_ or {})
    cur = meta.get("centerId")
    if cur in ids:
        return
    if ids:
        meta["centerId"] = ids[0]
    else:
        meta.pop("centerId", None)
    user.metadata_ = meta
    flag_modified(user, "metadata_")


def unlink_center_everywhere(db: Session, center_id: str) -> None:
    """A centre was deleted: drop its links and move affected accounts' primary centre."""
    affected = {r.user_id for r in db.query(UserCenterAccessDB).filter(UserCenterAccessDB.center_id == center_id).all()}
    db.query(UserCenterAccessDB).filter(UserCenterAccessDB.center_id == center_id).delete(synchronize_session=False)
    db.flush()
    for u in db.query(UserDB).filter(UserDB.role == "CENTER").all():
        if u.id in affected or (u.metadata_ or {}).get("centerId") == center_id:
            reassign_primary_center(db, u)
