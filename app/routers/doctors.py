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
        "photoUrl": file_storage.public_url(meta.get("photoUrl")),
        "yearsOfExperience": _stored_years(meta.get("yearsOfExperience")),
        "expertise": _stored_expertise(meta.get("expertise")),
        "cvUrl": file_storage.public_url(meta.get("cvUrl")),
        "cvFileName": (meta.get("cvFileName") or "CV") if meta.get("cvUrl") else None,
    }


def _stored_years(value) -> Optional[int]:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _stored_expertise(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str) and v.strip()]

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
    merge_profile_media(
        meta_payload,
        doc_id,
        photo_url=doc_in.photoUrl,
        years=doc_in.yearsOfExperience,
        expertise=doc_in.expertise,
        cv_url=doc_in.cvUrl,
        cv_file_name=doc_in.cvFileName,
    )

    if existing:
        existing.first_name = doc_in.firstName
        existing.last_name = doc_in.lastName
        existing.full_name = doc_in.fullName
        existing.email = doc_in.email
        existing.username = doc_in.username
        existing.contact_number = doc_in.contactNumber
        # keep keys this form does not carry (photo, CV, experience, expertise...)
        existing.metadata_ = {**(existing.metadata_ or {}), **meta_payload}
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
SIGNATURE_MAX_BYTES = 5 * 1024 * 1024
SIGNATURE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_SIG_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.DOTALL)


def signature_filename_hint() -> str:
    """Unique name per upload under doctors/{id}/signature, so a stored signature file is
    never overwritten and reports signed earlier keep the image they were signed with."""
    return f"signature-{uuid.uuid4().hex[:12]}"


# Profile picture / CV / experience / expertise (My Profile). Same rules as the signature:
# a data: URL is a new upload, "" removes it, any other value keeps the current file.
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_MIME_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
CV_MAX_BYTES = 5 * 1024 * 1024
CV_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
MAX_YEARS_OF_EXPERIENCE = 70
MAX_EXPERTISE_TAGS = 20
MAX_EXPERTISE_TAG_LEN = 40


def _magic_ok(mime: str, data: bytes) -> bool:
    """The bytes must really be the declared type (not just a renamed file)."""
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "application/pdf":
        return data.startswith(b"%PDF-")
    if mime == "application/msword":
        return data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if mime.endswith("wordprocessingml.document"):
        return data.startswith(b"PK\x03\x04")
    return False


def _decode_upload(raw: str, allowed: dict, max_bytes: int, label: str, kinds: str, limit: str) -> tuple[str, bytes]:
    m = _SIG_DATA_URL_RE.match(raw)
    mime = m.group("mime").lower() if m else ""
    if not m or mime not in allowed:
        raise HTTPException(status_code=400, detail=f"{label} must be {kinds}")
    try:
        data = base64.b64decode(m.group("data"), validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{label} could not be read")
    if not data:
        raise HTTPException(status_code=400, detail=f"{label} is empty")
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail=f"{label} must be {limit} or smaller")
    if not _magic_ok(mime, data):
        raise HTTPException(status_code=400, detail=f"{label} does not look like {kinds}")
    return mime, data


def _clean_expertise(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        tag = re.sub(r"\s+", " ", str(v or "")).strip()
        if not tag:
            continue
        if len(tag) > MAX_EXPERTISE_TAG_LEN:
            raise HTTPException(status_code=400, detail=f"Each expertise must be {MAX_EXPERTISE_TAG_LEN} characters or fewer")
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    if len(out) > MAX_EXPERTISE_TAGS:
        raise HTTPException(status_code=400, detail=f"Add at most {MAX_EXPERTISE_TAGS} expertise areas")
    return out


def merge_profile_media(
    meta: dict,
    doc_id: str,
    *,
    photo_url=None,
    years=None,
    expertise=None,
    cv_url=None,
    cv_file_name=None,
) -> dict:
    """Attach photo / experience / expertise / CV from a create or edit payload."""
    if years is not None:
        try:
            years_n = int(years)
        except (TypeError, ValueError):
            years_n = None
        if years_n is not None and 0 <= years_n <= MAX_YEARS_OF_EXPERIENCE:
            meta["yearsOfExperience"] = years_n
    if expertise is not None:
        try:
            meta["expertise"] = _clean_expertise(expertise)
        except HTTPException:
            meta["expertise"] = [str(v).strip() for v in expertise if str(v).strip()][:MAX_EXPERTISE_TAGS]
    raw_photo = (photo_url or "").strip() if isinstance(photo_url, str) else None
    if raw_photo == "":
        pass
    elif raw_photo and raw_photo.startswith("data:"):
        mime, data = _decode_upload(
            raw_photo, PHOTO_MIME_TYPES, PHOTO_MAX_BYTES, "Profile picture", "a PNG, JPG or WEBP image", "5 MB"
        )
        meta["photoUrl"] = file_storage.save_bytes(
            data,
            category="doctors",
            entity_id=doc_id,
            subfolder="photo",
            filename=f"photo-{uuid.uuid4().hex[:12]}{PHOTO_MIME_TYPES[mime]}",
            content_type=mime,
        )
    elif raw_photo:
        stored = file_storage.materialize_media_reference(
            raw_photo, category="doctors", entity_id=doc_id, subfolder="photo", filename_hint="photo.png"
        )
        if stored:
            meta["photoUrl"] = stored
    raw_cv = (cv_url or "").strip() if isinstance(cv_url, str) else None
    if raw_cv == "":
        pass
    elif raw_cv and raw_cv.startswith("data:"):
        mime, data = _decode_upload(raw_cv, CV_MIME_TYPES, CV_MAX_BYTES, "CV", "a PDF, DOC or DOCX file", "5 MB")
        ext = CV_MIME_TYPES[mime]
        meta["cvUrl"] = file_storage.save_bytes(
            data,
            category="doctors",
            entity_id=doc_id,
            subfolder="cv",
            filename=f"cv-{uuid.uuid4().hex[:12]}{ext}",
            content_type=mime,
        )
        meta["cvFileName"] = _clean_cv_name(cv_file_name, ext)
    elif raw_cv:
        stored = file_storage.materialize_media_reference(
            raw_cv, category="doctors", entity_id=doc_id, subfolder="cv", filename_hint="cv.pdf"
        )
        if stored:
            meta["cvUrl"] = stored
            if cv_file_name:
                meta["cvFileName"] = str(cv_file_name)[:150]
    return meta


def _clean_cv_name(name: Optional[str], ext: str) -> str:
    base = re.split(r"[\\/]", (name or "").strip())[-1]
    base = re.sub(r"[\x00-\x1f<>:\"|?*]+", "", base).strip(" .")[:150]
    if not base:
        base = "CV"
    if not base.lower().endswith(ext):
        base = re.sub(r"\.(pdf|docx?|bin)$", "", base, flags=re.I) + ext
    return base


class DoctorProfileUpdate(BaseModel):
    degree: Optional[str] = None
    registrationNumber: Optional[str] = None
    # data: URL = new upload; "" = remove; any other value = keep the current image
    signatureUrl: Optional[str] = None
    photoUrl: Optional[str] = None
    # sent (number or null) = set / clear; omitted = keep
    yearsOfExperience: Optional[int] = None
    expertise: Optional[list[str]] = None
    cvUrl: Optional[str] = None
    cvFileName: Optional[str] = None


def _apply_profile_update(db: Session, doc: DoctorDB, body: DoctorProfileUpdate) -> dict:
    meta = dict(doc.metadata_ or {})
    fields_set = getattr(body, "model_fields_set", None) or getattr(body, "__fields_set__", set())
    # Validate everything first so a bad field never leaves a half-saved upload behind.
    if "yearsOfExperience" in fields_set:
        years = body.yearsOfExperience
        if years is not None and not (0 <= years <= MAX_YEARS_OF_EXPERIENCE):
            raise HTTPException(status_code=400, detail=f"Years of experience must be between 0 and {MAX_YEARS_OF_EXPERIENCE}")
        meta["yearsOfExperience"] = years
    if body.expertise is not None:
        meta["expertise"] = _clean_expertise(body.expertise)
    photo_raw = (body.photoUrl or "").strip() if body.photoUrl is not None else None
    photo_upload = None
    if photo_raw and photo_raw.startswith("data:"):
        photo_upload = _decode_upload(
            photo_raw, PHOTO_MIME_TYPES, PHOTO_MAX_BYTES, "Profile picture", "a PNG, JPG or WEBP image", "5 MB"
        )
    cv_raw = (body.cvUrl or "").strip() if body.cvUrl is not None else None
    cv_upload = None
    if cv_raw and cv_raw.startswith("data:"):
        cv_upload = _decode_upload(cv_raw, CV_MIME_TYPES, CV_MAX_BYTES, "CV", "a PDF, DOC or DOCX file", "5 MB")

    if photo_raw == "":
        meta["photoUrl"] = None
    elif photo_upload:
        mime, data = photo_upload
        meta["photoUrl"] = file_storage.save_bytes(
            data,
            category="doctors",
            entity_id=doc.id,
            subfolder="photo",
            filename=f"photo-{uuid.uuid4().hex[:12]}{PHOTO_MIME_TYPES[mime]}",
            content_type=mime,
        )
    if cv_raw == "":
        meta["cvUrl"] = None
        meta["cvFileName"] = None
    elif cv_upload:
        mime, data = cv_upload
        ext = CV_MIME_TYPES[mime]
        meta["cvUrl"] = file_storage.save_bytes(
            data,
            category="doctors",
            entity_id=doc.id,
            subfolder="cv",
            filename=f"cv-{uuid.uuid4().hex[:12]}{ext}",
            content_type=mime,
        )
        meta["cvFileName"] = _clean_cv_name(body.cvFileName, ext)
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
                raise HTTPException(status_code=400, detail="Signature image must be 5 MB or smaller")
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


# Doctors directory (cards on /dashboard/doctors) with the profile details.
# Same audience as that page: Super Admin, Manager and centre logins (not doctors).
DIRECTORY_ROLES = ("SUPER_ADMIN", "MANAGER", "CENTER")


@router.get("/directory", response_model=list[DoctorResponse])
def get_doctor_directory(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles(*DIRECTORY_ROLES)),
):
    docs = db.query(DoctorDB).order_by(DoctorDB.full_name).all()
    rows = []
    for d in docs:
        row = db_to_schema(d)
        if current_user.role not in ("SUPER_ADMIN", "MANAGER"):
            row["username"] = None  # login names are for the admins only
        rows.append(row)
    return rows


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
