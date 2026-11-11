from app.profile_documents import ProfileDocument, public_documents, prepare_documents, store_documents
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
try:
    from app.database import get_db
    from app.models import CenterDB, UserDB
    from app.schemas import CenterCreate, CenterResponse, CenterReportLayout
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
except ImportError:
    from app.database import get_db
    from app.models import CenterDB, UserDB
    from app.schemas import CenterCreate, CenterResponse, CenterReportLayout
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional
from pydantic import BaseModel
try:
    from app import storage as file_storage
    from app import access as access_svc
except ImportError:
    from app import storage as file_storage
    from app import access as access_svc

from app.manager_access import require_manager_perm

router = APIRouter(
    prefix="/centers",
    tags=["Radiology Centers"],
    dependencies=[Depends(get_current_user)],
)

def db_to_schema(c: CenterDB) -> dict:
    meta = c.metadata_ or {}
    return {
        "id": c.id,
        "centerName": c.center_name,
        "firstName": meta.get("firstName"),
        "lastName": meta.get("lastName"),
        "email": c.email,
        "username": meta.get("username"),
        "password": None,
        "contactNumber": c.contact_number,
        "address": meta.get("address"),
        "headerTemplateUrl": file_storage.public_url(meta.get("headerTemplateUrl")),
        "letterheadMode": meta.get("letterheadMode", "full-page"),
        "reportLayout": meta.get("reportLayout"),
        "documents": public_documents(meta.get("documents")),
        "logoUrl": file_storage.public_url(meta.get("logoUrl")),
        "createdAt": c.created_at,
        "frozenAt": getattr(c, "frozen_at", None),
    }

@router.get("", response_model=list[CenterResponse])
def get_centers(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Every centre for staff and doctors; a centre-side login only sees the centres it is linked to."""
    try:
        from app.soft_delete import active_filter
    except ImportError:
        from app.soft_delete import active_filter
    q = active_filter(db.query(CenterDB), CenterDB)
    ids = access_svc.linked_center_ids(db, current_user) if current_user.role == "CENTER" else None
    if ids is not None:
        if not ids:
            return []
        q = q.filter(CenterDB.id.in_(ids))
    return [db_to_schema(c) for c in q.all()]


class CenterProfileUpdate(BaseModel):
    centerName: Optional[str] = None
    contactNumber: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    headerTemplateUrl: Optional[str] = None
    letterheadMode: Optional[str] = None
    reportLayout: Optional[CenterReportLayout] = None
    logoUrl: Optional[str] = None
    documents: Optional[list[ProfileDocument]] = None


@router.put("/{center_id}/profile", response_model=CenterResponse)
def update_center_profile(
    center_id: str,
    body: CenterProfileUpdate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Centre details / letterhead. Super Admin, or a centre login with Center Info = write
    for this centre. Only the fields sent are changed; the centre's login is not touched."""
    if current_user.role not in ("SUPER_ADMIN", "CENTER"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    access_svc.require_center_perm(db, current_user, center_id, "center_info", "write")
    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Center not found")
    meta = dict(c.metadata_ or {})
    documents = prepare_documents(body.documents, meta.get("documents")) if body.documents is not None else None
    if body.centerName is not None:
        name = " ".join(body.centerName.split())
        if not name:
            raise HTTPException(status_code=400, detail="Centre name is required")
        c.center_name = name
    if body.contactNumber is not None:
        c.contact_number = body.contactNumber.strip()
    if body.email is not None:
        c.email = body.email.strip()
    if body.address is not None:
        meta["address"] = body.address.strip()
    if body.headerTemplateUrl is not None:
        meta["headerTemplateUrl"] = file_storage.materialize_media_reference(
            body.headerTemplateUrl, category="centers", entity_id=center_id,
            subfolder="header", filename_hint="header.png",
        ) if body.headerTemplateUrl else None
    if body.letterheadMode is not None:
        if body.letterheadMode not in ("header", "footer", "full-page", "preprinted"):
            raise HTTPException(status_code=400, detail="Invalid letterhead placement")
        meta["letterheadMode"] = body.letterheadMode
    if body.reportLayout is not None:
        meta["reportLayout"] = body.reportLayout.model_dump()
    if body.logoUrl is not None:
        meta["logoUrl"] = file_storage.materialize_media_reference(
            body.logoUrl, category="centers", entity_id=center_id,
            subfolder="logo", filename_hint="logo.png",
        ) if body.logoUrl else None
    if documents is not None:
        meta["documents"] = store_documents(documents, "centers", center_id)
    c.metadata_ = meta
    flag_modified(c, "metadata_")
    db.commit()
    db.refresh(c)
    return db_to_schema(c)


class ReferrerIn(BaseModel):
    name: str
    # Referral cut for this referring doctor (optional)
    hasCut: bool = False
    cutType: Optional[str] = None  # "percent" | "fixed"
    cutValue: Optional[float] = None


def _referrers(c: CenterDB) -> list:
    return list((c.metadata_ or {}).get("referringDoctors") or [])


@router.get("/{center_id}/referrers")
def list_referrers(center_id: str, db: Session = Depends(get_db), current_user: UserDB = Depends(get_current_user)):
    """Referring doctors saved for a centre (stored in centers.metadata.referringDoctors)."""
    if current_user.role not in ("SUPER_ADMIN", "MANAGER", "CENTER"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if current_user.role == "CENTER":
        access_svc.require_center_perm(db, current_user, center_id, "reports", "read")
    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Center not found")
    return _referrers(c)


@router.post("/{center_id}/referrers")
def add_referrer(center_id: str, body: ReferrerIn, db: Session = Depends(get_db), current_user: UserDB = Depends(get_current_user)):
    """Add (or update by name) a centre's referring doctor, with an optional referral cut."""
    # MGRPERM:add_referrer
    if current_user.role == "MANAGER":
        raise HTTPException(status_code=403, detail="Managers cannot change saved referring doctors directly")
    if current_user.role not in ("SUPER_ADMIN", "MANAGER", "CENTER"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if current_user.role == "CENTER":
        access_svc.require_center_perm(db, current_user, center_id, "reports", "write")
    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Center not found")
    name = " ".join((body.name or "").split()).upper()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Referring doctor name is required")
    entry = {"name": name, "hasCut": bool(body.hasCut), "cutType": None, "cutValue": None}
    if body.hasCut:
        if body.cutType not in ("percent", "fixed"):
            raise HTTPException(status_code=400, detail="Choose % or fixed amount for the referral cut")
        v = float(body.cutValue or 0)
        if v < 0 or (body.cutType == "percent" and v > 100):
            raise HTTPException(status_code=400, detail="Invalid referral cut")
        entry.update(cutType=body.cutType, cutValue=round(v, 2))
    items = [r for r in _referrers(c) if (r.get("name") or "").upper() != name]
    items.append(entry)
    meta = dict(c.metadata_ or {})
    meta["referringDoctors"] = items
    c.metadata_ = meta
    flag_modified(c, "metadata_")
    db.commit()
    return entry


@router.post("", response_model=CenterResponse)
async def save_center(
    center_in: CenterCreate,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Super Admin saves directly. A Manager only stages the change (create or edit):
    it is applied when a Super Admin approves it (HTTP 202)."""
    # MGRPERM:save_center
    require_manager_perm(_current_user, "centers", "write")
    if _current_user.role == "MANAGER":
        exists = bool(center_in.id) and db.query(CenterDB).filter(CenterDB.id == center_in.id).first() is not None
        try:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        except ImportError:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        payload = center_in.model_dump() if hasattr(center_in, "model_dump") else center_in.dict()
        if payload.get("password") and not looks_like_bcrypt(payload["password"]):
            payload["password"] = hash_password(payload["password"])
        appr = create_pending_approval(
            db, _current_user, action_type="UPDATE_CENTER" if exists else "CREATE_CENTER", entity_type="center",
            entity_id=center_in.id if exists else None, payload=payload,
        )
        data = await announce_pending_approval(db, appr, _current_user)
        return pending_response(data, ("Edit" if exists else "New centre") + " sent to the Super Admin for approval.")
    try:
        from app.entity_lifecycle import write_audit, snapshot_center_row
        from app.soft_delete import is_deleted
    except ImportError:
        from app.entity_lifecycle import write_audit, snapshot_center_row
        from app.soft_delete import is_deleted
    before = None
    if center_in.id:
        prev = db.query(CenterDB).filter(CenterDB.id == center_in.id).first()
        if prev and not is_deleted(prev):
            before = snapshot_center_row(prev)
    saved = save_center_core(center_in, db)
    write_audit(
        db, actor=_current_user, entity_type="center", entity_id=saved["id"],
        action="UPDATE" if before else "CREATE", before=before,
        after=snapshot_center_row(db.query(CenterDB).filter(CenterDB.id == saved["id"]).first()),
        commit=True,
    )
    return saved


def save_center_core(center_in: CenterCreate, db: Session) -> dict:
    c_id = center_in.id or f"center-{int(time.time() * 1000)}"
    existing = db.query(CenterDB).filter(CenterDB.id == c_id).first()

    documents = prepare_documents(center_in.documents, (existing.metadata_ or {}).get("documents") if existing else None) if center_in.documents is not None else None
    hashed_pw = None
    if center_in.password:
        hashed_pw = center_in.password if looks_like_bcrypt(center_in.password) else hash_password(center_in.password)
    meta_payload = {
        "firstName": center_in.firstName,
        "lastName": center_in.lastName,
        "username": center_in.username,
        "password": hashed_pw,
        "address": center_in.address,
        "headerTemplateUrl": file_storage.materialize_media_reference(
            center_in.headerTemplateUrl,
            category="centers",
            entity_id=c_id,
            subfolder="header",
            filename_hint="header.png",
        ),
        "letterheadMode": center_in.letterheadMode or "full-page",
        "reportLayout": center_in.reportLayout.model_dump() if center_in.reportLayout is not None else None,
        "logoUrl": file_storage.materialize_media_reference(
            center_in.logoUrl,
            category="centers",
            entity_id=c_id,
            subfolder="logo",
            filename_hint="logo.png",
        ),
    }

    if documents is not None:
        meta_payload["documents"] = store_documents(documents, "centers", c_id)

    if existing:
        existing.center_name = center_in.centerName
        existing.email = center_in.email
        existing.contact_number = center_in.contactNumber
        # Edit = merge: fields not sent (None) keep their stored value, so an edit
        # never wipes the login password, letterhead, logo or saved referrers.
        merged = dict(existing.metadata_ or {})
        if center_in.letterheadMode is None:
            meta_payload.pop("letterheadMode", None)
        for k, v in meta_payload.items():
            if v is not None:
                merged[k] = v
        existing.metadata_ = merged
        flag_modified(existing, "metadata_")
        db.commit()
        db.refresh(existing)
        saved_c = existing
    else:
        created_at = datetime.utcnow().strftime("%Y-%m-%d")
        new_c = CenterDB(
            id=c_id,
            center_name=center_in.centerName,
            email=center_in.email,
            contact_number=center_in.contactNumber,
            created_at=created_at,
            metadata_=meta_payload,
        )
        db.add(new_c)
        db.commit()
        db.refresh(new_c)
        saved_c = new_c

    # Auto-register user account for Center login (password stored as bcrypt)
    login_email = center_in.username or saved_c.email
    if login_email and hashed_pw:
        existing_user = db.query(UserDB).filter(UserDB.email.ilike(login_email)).first()
        if existing_user:
            user_meta = dict(existing_user.metadata_ or {})
            user_meta["password"] = hashed_pw
            user_meta["centerId"] = saved_c.id
            existing_user.name = saved_c.center_name
            existing_user.metadata_ = user_meta
            flag_modified(existing_user, "metadata_")
        else:
            existing_user = UserDB(
                email=login_email,
                name=saved_c.center_name,
                role="CENTER",
                metadata_={"password": hashed_pw, "centerId": saved_c.id}
            )
            db.add(existing_user)
        db.flush()
        # The centre's own login is Center Admin of that centre (full page rights)
        access_svc.ensure_primary_center_admin(db, existing_user, saved_c.id, created_by="center-save")
        db.commit()

    return db_to_schema(saved_c)

class FreezeBody(BaseModel):
    frozen: bool


@router.put("/{center_id}/freeze", response_model=CenterResponse)
async def freeze_center(
    center_id: str,
    body: FreezeBody,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Freeze/unfreeze a centre. Manager stages for Super Admin approval (202)."""
    # MGRPERM:freeze_center
    require_manager_perm(_current_user, "centers", "write")
    try:
        from app.soft_delete import is_deleted
        from app.entity_lifecycle import set_center_frozen
    except ImportError:
        from app.soft_delete import is_deleted
        from app.entity_lifecycle import set_center_frozen
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not center or is_deleted(center):
        raise HTTPException(status_code=404, detail="Center not found")
    if _current_user.role == "MANAGER":
        try:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response,
            )
        except ImportError:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response,
            )
        action = "FREEZE_CENTER" if body.frozen else "UNFREEZE_CENTER"
        appr = create_pending_approval(
            db, _current_user, action_type=action, entity_type="center",
            entity_id=center_id, payload={"id": center_id, "frozen": bool(body.frozen)},
        )
        data = await announce_pending_approval(db, appr, _current_user)
        return pending_response(
            data,
            ("Freeze" if body.frozen else "Unfreeze") + " request sent to the Super Admin for approval.",
        )
    try:
        set_center_frozen(db, center_id, bool(body.frozen), actor=_current_user)
    except LookupError:
        raise HTTPException(status_code=404, detail="Center not found")
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    return db_to_schema(center)


@router.delete("/{center_id}", status_code=204)
async def delete_center(
    center_id: str,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Only Super Admin and Manager may delete. A Manager's delete waits for Super Admin approval (202)."""
    # MGRPERM:delete_center
    require_manager_perm(_current_user, "centers", "delete")
    try:
        from app.soft_delete import is_deleted
        from app.entity_lifecycle import write_audit, snapshot_center_row
    except ImportError:
        from app.soft_delete import is_deleted
        from app.entity_lifecycle import write_audit, snapshot_center_row
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not center or is_deleted(center):
        raise HTTPException(status_code=404, detail="Center not found")
    if _current_user.role == "MANAGER":
        try:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_center,
            )
        except ImportError:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_center,
            )
        appr = create_pending_approval(
            db, _current_user, action_type="DELETE_CENTER", entity_type="center",
            entity_id=center_id, payload=_snapshot_center(center_id, db) or {"id": center_id},
        )
        data = await announce_pending_approval(db, appr, _current_user)
        return pending_response(data, "Delete request sent to the Super Admin for approval.")
    before = snapshot_center_row(center)
    delete_center_core(db, center_id)
    write_audit(
        db, actor=_current_user, entity_type="center", entity_id=center_id,
        action="DELETE", before=before, after=None, commit=True,
    )
    return None


def delete_center_core(db: Session, center_id: str) -> None:
    """Soft-delete the centre. Access links cleared; orphaned centre logins
    (no remaining live centre) are soft-deleted so they cannot sign in or appear
    under Center Users. Pricing rows kept. Cases and invoices stay for history."""
    try:
        from app.soft_delete import soft_delete_center_and_orphaned_logins
    except ImportError:
        from app.soft_delete import soft_delete_center_and_orphaned_logins
    soft_delete_center_and_orphaned_logins(db, center_id)
    db.commit()
