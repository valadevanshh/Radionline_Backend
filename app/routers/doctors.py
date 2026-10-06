import base64
import re
import time
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
try:
    from app.database import get_db
    from app.models import DoctorDB, UserDB
    from app.schemas import DoctorCreate, DoctorResponse
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
except ImportError:
    from app.database import get_db
    from app.models import DoctorDB, UserDB
    from app.schemas import DoctorCreate, DoctorResponse
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
from sqlalchemy.orm.attributes import flag_modified
try:
    from app import storage as file_storage
except ImportError:
    from app import storage as file_storage

router = APIRouter(
    prefix="/doctors",
    tags=["Doctors"],
    dependencies=[Depends(get_current_user)],
)

def db_to_schema(d: DoctorDB) -> dict:
    meta = d.metadata_ or {}
    return {
        "id": d.id,
        "firstName": d.first_name,
        "lastName": d.last_name,
        "fullName": d.full_name,
        "email": d.email,
        "username": d.username,
        "password": None,
        "contactNumber": d.contact_number,
        "address": meta.get("address"),
        "signatureUrl": file_storage.public_url(meta.get("signatureUrl")),
        "profileFileUrl": file_storage.public_url(meta.get("profileFileUrl")),
        "degree": meta.get("degree", "M.D. (Radiodiagnosis)"),
        "registrationNumber": meta.get("registrationNumber"),
        "createdAt": d.created_at,
    }

@router.get("", response_model=list[DoctorResponse])
def get_doctors(db: Session = Depends(get_db)):
    docs = db.query(DoctorDB).all()
    return [db_to_schema(d) for d in docs]

@router.post("", response_model=DoctorResponse)
async def save_doctor(
    doc_in: DoctorCreate,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Super Admin saves directly. A Manager only stages the change (create or edit):
    it is applied when a Super Admin approves it (HTTP 202)."""
    if _current_user.role == "MANAGER":
        exists = bool(doc_in.id) and db.query(DoctorDB).filter(DoctorDB.id == doc_in.id).first() is not None
        try:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        except ImportError:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        payload = doc_in.model_dump() if hasattr(doc_in, "model_dump") else doc_in.dict()
        if payload.get("password") and not looks_like_bcrypt(payload["password"]):
            payload["password"] = hash_password(payload["password"])
        appr = create_pending_approval(
            db, _current_user, action_type="UPDATE_DOCTOR" if exists else "CREATE_DOCTOR", entity_type="doctor",
            entity_id=doc_in.id if exists else None, payload=payload,
        )
        data = await announce_pending_approval(db, appr, _current_user)
        return pending_response(data, ("Edit" if exists else "New doctor") + " sent to the Super Admin for approval.")
    return save_doctor_core(doc_in, db)


def save_doctor_core(doc_in: DoctorCreate, db: Session) -> dict:
    doc_id = doc_in.id or f"doc-{int(time.time() * 1000)}"
    existing = db.query(DoctorDB).filter(DoctorDB.id == doc_id).first()

    hashed_pw = None
    if doc_in.password:
        hashed_pw = doc_in.password if looks_like_bcrypt(doc_in.password) else hash_password(doc_in.password)
    meta_payload = {
        "password": hashed_pw,
        "address": doc_in.address,
        "signatureUrl": file_storage.materialize_media_reference(
            doc_in.signatureUrl,
            category="doctors",
            entity_id=doc_id,
            subfolder="signature",
            filename_hint=signature_filename_hint(),
        ),
        "profileFileUrl": file_storage.materialize_media_reference(
            doc_in.profileFileUrl,
            category="doctors",
            entity_id=doc_id,
            subfolder="profile",
            filename_hint="profile.png",
        ),
        "degree": doc_in.degree or "M.D. (Radiodiagnosis)",
        "registrationNumber": doc_in.registrationNumber,
    }

    if existing:
        existing.first_name = doc_in.firstName
        existing.last_name = doc_in.lastName
        existing.full_name = doc_in.fullName
        existing.email = doc_in.email
        existing.username = doc_in.username
        existing.contact_number = doc_in.contactNumber
        existing.metadata_ = meta_payload
        db.commit()
        db.refresh(existing)
        saved_doc = existing
    else:
        created_at = datetime.utcnow().strftime("%Y-%m-%d")
        new_doc = DoctorDB(
            id=doc_id,
            first_name=doc_in.firstName,
            last_name=doc_in.lastName,
            full_name=doc_in.fullName,
            email=doc_in.email,
            username=doc_in.username,
            contact_number=doc_in.contactNumber,
            created_at=created_at,
            metadata_=meta_payload,
        )
        db.add(new_doc)
        db.commit()
        db.refresh(new_doc)
        saved_doc = new_doc

    # Auto-register user account for Doctor login (password stored as bcrypt)
    login_email = saved_doc.username or saved_doc.email
    if login_email and hashed_pw:
        existing_user = db.query(UserDB).filter(UserDB.email.ilike(login_email)).first()
        if existing_user:
            user_meta = dict(existing_user.metadata_ or {})
            user_meta["password"] = hashed_pw
            user_meta["doctorId"] = saved_doc.id
            existing_user.name = saved_doc.full_name
            existing_user.metadata_ = user_meta
            flag_modified(existing_user, "metadata_")
        else:
            new_u = UserDB(
                email=login_email,
                name=saved_doc.full_name,
                role="DOCTOR",
                metadata_={"password": hashed_pw, "doctorId": saved_doc.id}
            )
            db.add(new_u)
        db.commit()

    return db_to_schema(saved_doc)

# ---------------------------------------------------------------------------
# Report signature details: degree / registration number / signature image.
# The doctor edits their own; the Super Admin can edit any doctor's. Only these
# keys are changed (the rest of the doctor record is left as it is).
# ---------------------------------------------------------------------------
SIGNATURE_MAX_BYTES = 2 * 1024 * 1024
SIGNATURE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_SIG_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.DOTALL)


def signature_filename_hint() -> str:
    """Unique name per upload under doctors/{id}/signature, so a stored signature file is
    never overwritten and reports signed earlier keep the image they were signed with."""
    return f"signature-{uuid.uuid4().hex[:12]}"


class DoctorProfileUpdate(BaseModel):
    degree: Optional[str] = None
    registrationNumber: Optional[str] = None
    # data: URL = new upload; "" = remove; any other value = keep the current image
    signatureUrl: Optional[str] = None


def _apply_profile_update(db: Session, doc: DoctorDB, body: DoctorProfileUpdate) -> dict:
    meta = dict(doc.metadata_ or {})
    if body.degree is not None:
        meta["degree"] = body.degree.strip()[:120]
    if body.registrationNumber is not None:
        meta["registrationNumber"] = body.registrationNumber.strip()[:80] or None
    if body.signatureUrl is not None:
        raw = body.signatureUrl.strip()
        if not raw:
            meta["signatureUrl"] = None
        elif raw.startswith("data:"):
            m = _SIG_DATA_URL_RE.match(raw)
            if not m or m.group("mime").lower() not in SIGNATURE_MIME_TYPES:
                raise HTTPException(status_code=400, detail="Signature must be a PNG, JPG or WEBP image")
            try:
                size = len(base64.b64decode(m.group("data"), validate=False))
            except Exception:
                raise HTTPException(status_code=400, detail="Signature image could not be read")
            if size > SIGNATURE_MAX_BYTES:
                raise HTTPException(status_code=400, detail="Signature image must be 2 MB or smaller")
            meta["signatureUrl"] = file_storage.materialize_media_reference(
                raw,
                category="doctors",
                entity_id=doc.id,
                subfolder="signature",
                filename_hint=signature_filename_hint(),
            )
    doc.metadata_ = meta
    flag_modified(doc, "metadata_")
    db.commit()
    db.refresh(doc)
    return db_to_schema(doc)


def _own_doctor(db: Session, user: UserDB) -> DoctorDB:
    doctor_id = (user.metadata_ or {}).get("doctorId")
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first() if doctor_id else None
    if not doc:
        raise HTTPException(status_code=404, detail="No doctor profile is linked to this login")
    return doc


@router.get("/me", response_model=DoctorResponse)
def get_my_doctor_profile(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    return db_to_schema(_own_doctor(db, current_user))


@router.put("/me/profile", response_model=DoctorResponse)
def update_my_doctor_profile(
    body: DoctorProfileUpdate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    return _apply_profile_update(db, _own_doctor(db, current_user), body)


@router.put("/{doctor_id}/profile", response_model=DoctorResponse)
def update_doctor_profile(
    doctor_id: str,
    body: DoctorProfileUpdate,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return _apply_profile_update(db, doc, body)


@router.delete("/{doctor_id}", status_code=204)
async def delete_doctor(
    doctor_id: str,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Only Super Admin and Manager may delete. A Manager's delete waits for Super Admin approval (202)."""
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if _current_user.role == "MANAGER":
        try:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_doctor,
            )
        except ImportError:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_doctor,
            )
        appr = create_pending_approval(
            db, _current_user, action_type="DELETE_DOCTOR", entity_type="doctor",
            entity_id=doctor_id, payload=_snapshot_doctor(doctor_id, db) or {"id": doctor_id},
        )
        data = await announce_pending_approval(db, appr, _current_user)
        return pending_response(data, "Delete request sent to the Super Admin for approval.")
    delete_doctor_core(db, doctor_id)
    return None


def delete_doctor_core(db: Session, doctor_id: str) -> None:
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if doc:
        db.delete(doc)
    db.commit()
