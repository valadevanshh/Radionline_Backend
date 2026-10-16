import time
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

try:
    from app.database import get_db, engine
    from app.models import TemplateDB, UserDB, DoctorDB
    from app.schemas import TemplateCreate, TemplateResponse
    from app.security import get_current_user, require_roles
    from app import access as access_svc
except ImportError:
    from app.database import get_db, engine
    from app.models import TemplateDB, UserDB, DoctorDB
    from app.schemas import TemplateCreate, TemplateResponse
    from app.security import get_current_user, require_roles
    from app import access as access_svc

router = APIRouter(
    prefix="/templates",
    tags=["Templates"],
    dependencies=[Depends(get_current_user)],
)


def ensure_template_columns(bind):
    try:
        with bind.begin() as conn:
            for col, ddl in [("doctor_id", "VARCHAR(100)"), ("doctor_name", "VARCHAR(255)")]:
                try:
                    conn.execute(sa_text(f"ALTER TABLE doc_templates ADD COLUMN IF NOT EXISTS {col} {ddl}"))
                except Exception:
                    try:
                        conn.execute(sa_text(f"ALTER TABLE doc_templates ADD COLUMN {col} {ddl}"))
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"ensure_template_columns skipped: {e}")


# Ensure columns exist on startup
try:
    ensure_template_columns(engine)
except Exception as e:
    logger.warning(f"ensure_template_columns invocation skipped: {e}")


def _template_doctor_id(t: TemplateDB) -> Optional[str]:
    doc_id = getattr(t, "doctor_id", None)
    if doc_id:
        return str(doc_id).strip()
    meta = t.metadata_ or {}
    m_id = meta.get("doctorId") or meta.get("createdByDoctorId")
    if m_id:
        return str(m_id).strip()
    return None


def _doctor_claim_identity_keys(db: Session, user: UserDB) -> set:
    meta = user.metadata_ or {}
    doctor_id = meta.get("doctorId") or str(user.id)
    keys = {str(doctor_id), str(user.id)}
    if user.email:
        keys.add(user.email.strip().lower())
    if getattr(user, "name", None):
        keys.add(user.name.strip().lower())
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first() if doctor_id else None
    if doc:
        if doc.email:
            keys.add(doc.email.strip().lower())
        if doc.username:
            keys.add(doc.username.strip().lower())
        doc_name = getattr(doc, "full_name", None) or getattr(doc, "name", None)
        if doc_name:
            keys.add(doc_name.strip().lower())
    return keys


def db_to_schema(t: TemplateDB) -> dict:
    meta = t.metadata_ or {}
    return {
        "id": t.id,
        "title": t.title,
        "centerId": t.center_id,
        "centerName": t.center_name,
        "modality": t.modality,
        "bodyPart": meta.get("bodyPart"),
        "findings": meta.get("findings"),
        "impression": meta.get("impression"),
        "content": meta.get("content"),
        "doctorId": getattr(t, "doctor_id", None) or meta.get("doctorId") or meta.get("createdByDoctorId"),
        "doctorName": getattr(t, "doctor_name", None) or meta.get("doctorName") or meta.get("createdByDoctorName"),
        "createdAt": t.created_at,
    }


@router.get("", response_model=list[TemplateResponse])
def get_templates(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    templates = db.query(TemplateDB).order_by(TemplateDB.created_at.desc()).all()
    if current_user.role == "CENTER":
        # Centre logins: global templates + their own centres', only with Templates >= read
        ids = set(access_svc.center_ids_with(db, current_user, "templates", "read") or [])
        if not ids:
            return []
        # Centers must not see private templates created by doctors
        templates = [
            t for t in templates
            if not _template_doctor_id(t) and ((t.center_id or "ALL") == "ALL" or t.center_id in ids)
        ]
    elif current_user.role == "DOCTOR":
        doc_keys = _doctor_claim_identity_keys(db, current_user)
        visible = []
        for t in templates:
            t_doc = _template_doctor_id(t)
            # If template was created by a doctor, it is strictly visible ONLY to that creator doctor!
            if t_doc:
                if str(t_doc).strip().lower() in doc_keys or str(t_doc).strip() in doc_keys:
                    visible.append(t)
                # Doctor B cannot see Doctor A's templates
            else:
                # System standard / baseline templates are available to all doctors
                visible.append(t)
        templates = visible

    return [db_to_schema(t) for t in templates]


@router.get("/match")
def match_template(
    modality: str = Query(..., min_length=1),
    bodyPart: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Return first template whose modality + bodyPart equal the query (case-insensitive trim)."""
    mod_n = modality.strip().lower()
    bp_n = bodyPart.strip().lower()
    if not mod_n or not bp_n:
        return Response(status_code=204)

    templates = db.query(TemplateDB).order_by(TemplateDB.created_at.desc()).all()
    if current_user.role == "DOCTOR":
        doc_keys = _doctor_claim_identity_keys(db, current_user)
        templates = [
            t for t in templates
            if not _template_doctor_id(t) or (
                str(_template_doctor_id(t)).strip().lower() in doc_keys or
                str(_template_doctor_id(t)).strip() in doc_keys
            )
        ]
    elif current_user.role == "CENTER":
        ids = set(access_svc.center_ids_with(db, current_user, "templates", "read") or [])
        templates = [
            t for t in templates
            if not _template_doctor_id(t) and ((t.center_id or "ALL") == "ALL" or t.center_id in ids)
        ]

    for t in templates:
        if (t.modality or "").strip().lower() != mod_n:
            continue
        meta = t.metadata_ or {}
        t_bp = (meta.get("bodyPart") or "").strip().lower()
        if t_bp == bp_n:
            return db_to_schema(t)
    return Response(status_code=204)


@router.get("/{template_id}", response_model=TemplateResponse)
def get_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    tmpl = db.query(TemplateDB).filter(TemplateDB.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    t_doc = _template_doctor_id(tmpl)
    if current_user.role == "DOCTOR":
        doc_keys = _doctor_claim_identity_keys(db, current_user)
        if t_doc and str(t_doc).strip().lower() not in doc_keys and str(t_doc).strip() not in doc_keys:
            raise HTTPException(status_code=403, detail="Access denied: this template was created by another doctor")
    elif current_user.role == "CENTER":
        if t_doc:
            raise HTTPException(status_code=403, detail="Access denied")
        ids = set(access_svc.center_ids_with(db, current_user, "templates", "read") or [])
        if (tmpl.center_id or "ALL") != "ALL" and tmpl.center_id not in ids:
            raise HTTPException(status_code=403, detail="Access denied")
    return db_to_schema(tmpl)


@router.post("", response_model=TemplateResponse)
async def save_template(
    tmpl_in: TemplateCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Create / edit a template.
    Strictly limited to doctors only."""
    if current_user.role != "DOCTOR":
        raise HTTPException(
            status_code=403,
            detail="Template creation is limited to doctors only",
        )
    return save_template_core(tmpl_in, db, current_user)


def save_template_core(
    tmpl_in: TemplateCreate,
    db: Session,
    current_user: Optional[UserDB] = None,
) -> dict:
    t_id = tmpl_in.id or f"tmpl-{int(time.time() * 1000)}"
    existing = db.query(TemplateDB).filter(TemplateDB.id == t_id).first()

    creator_doctor_id = None
    creator_doctor_name = None
    if current_user and current_user.role == "DOCTOR":
        meta_u = current_user.metadata_ or {}
        creator_doctor_id = meta_u.get("doctorId") or str(current_user.id)
        doc = db.query(DoctorDB).filter(DoctorDB.id == creator_doctor_id).first() if creator_doctor_id else None
        doc_name = (getattr(doc, "full_name", None) or getattr(doc, "name", None)) if doc else None
        creator_doctor_name = doc_name or (current_user.name or current_user.email)
    elif tmpl_in.doctorId:
        creator_doctor_id = tmpl_in.doctorId
        creator_doctor_name = tmpl_in.doctorName

    if existing:
        # Check permissions if doctor is updating
        if current_user and current_user.role == "DOCTOR":
            ex_doc = _template_doctor_id(existing)
            if ex_doc:
                doc_keys = _doctor_claim_identity_keys(db, current_user)
                if str(ex_doc).strip().lower() not in doc_keys and str(ex_doc).strip() not in doc_keys:
                    raise HTTPException(
                        status_code=403,
                        detail="You cannot modify a template created by another doctor",
                    )
        existing.title = tmpl_in.title
        existing.center_id = tmpl_in.centerId
        existing.center_name = tmpl_in.centerName
        existing.modality = tmpl_in.modality
        if not getattr(existing, "doctor_id", None) and creator_doctor_id:
            existing.doctor_id = creator_doctor_id
        if not getattr(existing, "doctor_name", None) and creator_doctor_name:
            existing.doctor_name = creator_doctor_name

        meta_payload = {
            "bodyPart": tmpl_in.bodyPart,
            "findings": tmpl_in.findings,
            "impression": tmpl_in.impression,
            "content": tmpl_in.content,
            "doctorId": getattr(existing, "doctor_id", None) or creator_doctor_id,
            "doctorName": getattr(existing, "doctor_name", None) or creator_doctor_name,
        }
        existing.metadata_ = meta_payload
        db.commit()
        db.refresh(existing)
        return db_to_schema(existing)
    else:
        created_at = datetime.utcnow().isoformat() + "Z"
        meta_payload = {
            "bodyPart": tmpl_in.bodyPart,
            "findings": tmpl_in.findings,
            "impression": tmpl_in.impression,
            "content": tmpl_in.content,
            "doctorId": creator_doctor_id,
            "doctorName": creator_doctor_name,
        }
        new_t = TemplateDB(
            id=t_id,
            title=tmpl_in.title,
            center_id=tmpl_in.centerId,
            center_name=tmpl_in.centerName,
            modality=tmpl_in.modality,
            doctor_id=creator_doctor_id,
            doctor_name=creator_doctor_name,
            created_at=created_at,
            metadata_=meta_payload,
        )
        db.add(new_t)
        db.commit()
        db.refresh(new_t)
        return db_to_schema(new_t)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    tmpl = db.query(TemplateDB).filter(TemplateDB.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if current_user.role == "DOCTOR":
        t_doc = _template_doctor_id(tmpl)
        doc_keys = _doctor_claim_identity_keys(db, current_user)
        if not t_doc or (str(t_doc).strip().lower() not in doc_keys and str(t_doc).strip() not in doc_keys):
            raise HTTPException(
                status_code=403,
                detail="You can only delete templates that you created",
            )
        delete_template_core(db, template_id)
        return None

    if current_user.role == "MANAGER":
        try:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_template,
            )
        except ImportError:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_template,
            )
        appr = create_pending_approval(
            db, current_user, action_type="DELETE_TEMPLATE", entity_type="template",
            entity_id=template_id, payload=_snapshot_template(template_id, db) or {"id": template_id},
        )
        data = await announce_pending_approval(db, appr, current_user)
        return pending_response(data, "Delete request sent to the Super Admin for approval.")

    if current_user.role == "SUPER_ADMIN":
        delete_template_core(db, template_id)
        return None

    raise HTTPException(status_code=403, detail="Permission denied")


def delete_template_core(db: Session, template_id: str) -> None:
    tmpl = db.query(TemplateDB).filter(TemplateDB.id == template_id).first()
    if tmpl:
        db.delete(tmpl)
    db.commit()
