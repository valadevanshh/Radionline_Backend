import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
try:
    from app.database import get_db
    from app.models import CenterDB, UserDB
    from app.schemas import CenterCreate, CenterResponse
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
except ImportError:
    from app.database import get_db
    from app.models import CenterDB, UserDB
    from app.schemas import CenterCreate, CenterResponse
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
        "logoUrl": file_storage.public_url(meta.get("logoUrl")),
        "createdAt": c.created_at,
    }

@router.get("", response_model=list[CenterResponse])
def get_centers(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Every centre for staff and doctors; a centre-side login only sees the centres it is linked to."""
    q = db.query(CenterDB)
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
    logoUrl: Optional[str] = None


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
    if body.logoUrl is not None:
        meta["logoUrl"] = file_storage.materialize_media_reference(
            body.logoUrl, category="centers", entity_id=center_id,
            subfolder="logo", filename_hint="logo.png",
        ) if body.logoUrl else None
    c.metadata_ = meta
    flag_modified(c, "metadata_")
    db.commit()
    db.refresh(c)
    return db_to_schema(c)


@router.post("", response_model=CenterResponse)
async def save_center(
    center_in: CenterCreate,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Super Admin saves directly. A Manager only stages the change (create or edit):
    it is applied when a Super Admin approves it (HTTP 202)."""
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
    return save_center_core(center_in, db)


def save_center_core(center_in: CenterCreate, db: Session) -> dict:
    c_id = center_in.id or f"center-{int(time.time() * 1000)}"
    existing = db.query(CenterDB).filter(CenterDB.id == c_id).first()

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
        "logoUrl": file_storage.materialize_media_reference(
            center_in.logoUrl,
            category="centers",
            entity_id=c_id,
            subfolder="logo",
            filename_hint="logo.png",
        ),
    }

    if existing:
        existing.center_name = center_in.centerName
        existing.email = center_in.email
        existing.contact_number = center_in.contactNumber
        existing.metadata_ = meta_payload
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

@router.delete("/{center_id}", status_code=204)
async def delete_center(
    center_id: str,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Only Super Admin and Manager may delete. A Manager's delete waits for Super Admin approval (202)."""
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not center:
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
    delete_center_core(db, center_id)
    return None


def delete_center_core(db: Session, center_id: str) -> None:
    """Delete the centre, its rate card and its user links. Cases, invoices and the
    centre's login rows stay (history); linked logins lose access to this centre."""
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if center:
        db.delete(center)
    try:
        from app.models import CenterPricingDB
    except ImportError:
        from app.models import CenterPricingDB
    db.query(CenterPricingDB).filter(CenterPricingDB.center_id == center_id).delete(synchronize_session=False)
    access_svc.unlink_center_everywhere(db, center_id)
    db.commit()
