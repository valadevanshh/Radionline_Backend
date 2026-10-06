import time
import uuid
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.database import get_db
    from app.models import (
        PendingApprovalDB, CaseDB, StudyDB, StudyImageDB, DoctorDB, CenterDB, TemplateDB, UserDB, ReportDB
    )
    from app.schemas import ApprovalSubmitRequest, ApprovalReviewRequest, ApprovalResponse
    from app.websocket import manager as ws_manager
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
    from app import pricing
    from app import notifications as notif_svc
except ImportError:
    from app.database import get_db
    from app.models import (
        PendingApprovalDB, CaseDB, StudyDB, StudyImageDB, DoctorDB, CenterDB, TemplateDB, UserDB, ReportDB
    )
    from app.schemas import ApprovalSubmitRequest, ApprovalReviewRequest, ApprovalResponse
    from app.websocket import manager as ws_manager
    from app.security import get_current_user, require_roles, hash_password, looks_like_bcrypt
    from app import pricing
    from app import notifications as notif_svc


router = APIRouter(
    prefix="/approvals",
    tags=["Approvals"],
    dependencies=[Depends(get_current_user)],
)

def _redact_sensitive(d: Optional[dict]) -> Optional[dict]:
    """Omit password values from approval diffs (never expose credentials)."""
    if d is None:
        return None
    out = dict(d)
    for key in list(out.keys()):
        lk = str(key).lower()
        if "password" in lk:
            val = out.get(key)
            out[key] = "***" if val not in (None, "", "***") else None
    return out


def _snapshot_case(case_id: str, db: Session) -> Optional[dict]:
    c = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    if not c:
        # Fall back to report row if case missing
        r = db.query(ReportDB).filter(ReportDB.id == case_id).first()
        if not r:
            return None
        meta = r.metadata_ or {}
        return {
            "id": r.id,
            "patientNumber": r.patient_number,
            "fullName": r.full_name,
            "age": r.age,
            "gender": r.gender,
            "phone": r.phone,
            "radiologyCenterId": r.radiology_center_id,
            "radiologyCenterName": r.radiology_center_name,
            "referringPhysicianId": r.referring_physician_id,
            "referringPhysicianName": r.referring_physician_name,
            "assignedDoctorId": r.assigned_doctor_id,
            "assignedDoctorName": r.assigned_doctor_name,
            "status": r.status,
            "isUrgent": bool(r.is_urgent) if r.is_urgent is not None else bool(meta.get("isUrgent", False)),
            "studyDate": r.study_date,
            "bodyParts": meta.get("bodyParts", []),
            "modality": meta.get("modality"),
            "clinicalNotes": meta.get("clinicalNotes"),
            "findings": meta.get("findings"),
            "impression": meta.get("impression"),
            "claimStatus": meta.get("claimStatus"),
            "claimedByDoctorId": meta.get("claimedByDoctorId"),
            "claimedByDoctorName": meta.get("claimedByDoctorName"),
        }
    meta = c.metadata_ or {}
    st = db.query(StudyDB).filter(StudyDB.case_id == case_id).first()
    return {
        "id": c.id,
        "patientNumber": c.patient_number,
        "fullName": c.full_name,
        "age": c.age,
        "ageUnit": meta.get("ageUnit") or "Years",
        "gender": c.gender,
        "phone": c.phone,
        "radiologyCenterId": c.radiology_center_id,
        "radiologyCenterName": c.radiology_center_name,
        "referringPhysicianId": c.referring_physician_id,
        "referringPhysicianName": c.referring_physician_name,
        "status": c.status,
        "isUrgent": bool(c.is_urgent) if c.is_urgent is not None else bool(meta.get("isUrgent", False)),
        "isPortable": bool(meta.get("isPortable", False)),
        "studyDate": c.study_date,
        "bodyParts": meta.get("bodyParts", []),
        "modality": meta.get("modality") or (st.modality if st else None),
        "clinicalNotes": meta.get("clinicalNotes") or (st.clinical_notes if st else None),
        "findings": meta.get("findings") or (st.findings if st else None),
        "impression": meta.get("impression") or (st.impression if st else None),
        "claimStatus": st.status if st else meta.get("claimStatus"),
        "claimedByDoctorId": (st.claimed_by if st else None) or meta.get("claimedByDoctorId"),
        "claimedByDoctorName": (st.claimed_by_name if st else None) or meta.get("claimedByDoctorName"),
        "docContent": (st.doc_content if st else None) or meta.get("docContent"),
    }


def _snapshot_doctor(doc_id: str, db: Session) -> Optional[dict]:
    d = db.query(DoctorDB).filter(DoctorDB.id == doc_id).first()
    if not d:
        return None
    meta = d.metadata_ or {}
    return {
        "id": d.id,
        "firstName": d.first_name,
        "lastName": d.last_name,
        "fullName": d.full_name,
        "email": d.email,
        "username": d.username,
        "contactNumber": d.contact_number,
        "address": meta.get("address"),
        "signatureUrl": meta.get("signatureUrl"),
        "profileFileUrl": meta.get("profileFileUrl"),
        "degree": meta.get("degree"),
        "registrationNumber": meta.get("registrationNumber"),
        "createdAt": d.created_at,
        # password intentionally omitted from before snapshot
    }


def _snapshot_center(center_id: str, db: Session) -> Optional[dict]:
    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not c:
        return None
    meta = c.metadata_ or {}
    return {
        "id": c.id,
        "centerName": c.center_name,
        "firstName": meta.get("firstName"),
        "lastName": meta.get("lastName"),
        "email": c.email,
        "username": meta.get("username"),
        "contactNumber": c.contact_number,
        "address": meta.get("address"),
        "headerTemplateUrl": meta.get("headerTemplateUrl"),
        "logoUrl": meta.get("logoUrl"),
        "createdAt": c.created_at,
    }


def _snapshot_template(tmpl_id: str, db: Session) -> Optional[dict]:
    t = db.query(TemplateDB).filter(TemplateDB.id == tmpl_id).first()
    if not t:
        return None
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
        "createdAt": t.created_at,
    }


def _current_entity_snapshot(entity_type: str, entity_id: Optional[str], action_type: str, db: Session) -> Optional[dict]:
    """Load current DB values for before/after diffs. CREATE has no before."""
    if not entity_id:
        return None
    at = (action_type or "").upper()
    if at.startswith("CREATE"):
        return None
    et = (entity_type or "").lower()
    if et == "case" or "CASE" in at:
        return _snapshot_case(entity_id, db)
    if et == "doctor" or "DOCTOR" in at:
        return _snapshot_doctor(entity_id, db)
    if et == "center" or "CENTER" in at:
        return _snapshot_center(entity_id, db)
    if et == "template" or "TEMPLATE" in at:
        return _snapshot_template(entity_id, db)
    return None


def db_to_schema(a: PendingApprovalDB, before: Optional[dict] = None) -> dict:
    return {
        "id": a.id,
        "managerId": a.manager_id,
        "managerName": a.manager_name,
        "actionType": a.action_type,
        "entityType": a.entity_type,
        "entityId": a.entity_id,
        "payload": _redact_sensitive(a.payload or {}),
        "before": _redact_sensitive(before) if before is not None else None,
        "status": a.status,
        "rejectionReason": a.rejection_reason,
        "createdAt": a.created_at,
        "reviewedAt": a.reviewed_at,
        "reviewedBy": a.reviewed_by,
    }

def create_pending_approval(
    db: Session,
    actor: UserDB,
    *,
    action_type: str,
    entity_type: str,
    entity_id: Optional[str],
    payload: Optional[dict],
) -> PendingApprovalDB:
    """Stage a change for Super Admin review (who asked comes from the login, never the body). Commits."""
    appr = PendingApprovalDB(
        id=f"appr-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
        manager_id=actor.email,
        manager_name=actor.name,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
        status="PENDING",
        created_at=datetime.utcnow().isoformat(),
    )
    db.add(appr)
    db.commit()
    db.refresh(appr)
    return appr


async def announce_pending_approval(db: Session, appr: PendingApprovalDB, actor: UserDB) -> dict:
    """Live event for open Approvals pages + a bell notification for every Super Admin."""
    schema_data = db_to_schema(appr)
    await ws_manager.broadcast({"type": "PENDING_APPROVAL_REQUEST", "approval": schema_data})
    if actor is not None and actor.role == "MANAGER":
        await notif_svc.notify_approval_requested(db, appr, actor)
    return schema_data


def pending_response(schema_data: dict, message: str):
    """HTTP 202 body returned to a Manager whose edit / delete now waits for a Super Admin."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content={"pendingApproval": True, "message": message, "approval": schema_data},
    )


@router.get("", response_model=List[ApprovalResponse])
def get_approvals(
    status: Optional[str] = Query(None),
    managerId: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Super Admin: every request. Manager: only their own submissions."""
    query = db.query(PendingApprovalDB)
    if status:
        query = query.filter(PendingApprovalDB.status == status)
    if current_user.role == "MANAGER":
        query = query.filter(PendingApprovalDB.manager_id == current_user.email)
    elif managerId:
        query = query.filter(PendingApprovalDB.manager_id == managerId)
    
    approvals = query.order_by(PendingApprovalDB.created_at.desc()).all()
    result = []
    for a in approvals:
        before = None
        # Only pending rows get a live DB snapshot (post-approve current != true before)
        if a.status == "PENDING":
            before = _current_entity_snapshot(a.entity_type, a.entity_id, a.action_type, db)
        result.append(db_to_schema(a, before=before))
    return result

@router.post("/submit", response_model=ApprovalResponse)
async def submit_approval(
    req: ApprovalSubmitRequest,
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("MANAGER", "SUPER_ADMIN")),
):
    appr = create_pending_approval(
        db,
        _user,
        action_type=req.actionType,
        entity_type=req.entityType,
        entity_id=req.entityId,
        payload=req.payload,
    )
    return await announce_pending_approval(db, appr, _user)

async def _apply_approved(appr: PendingApprovalDB, p: dict, db: Session, reviewer: UserDB) -> dict:
    """Apply a staged change. Returns a small summary used for the live events."""
    try:
        from app import case_ops
        from app.routers import reports as reports_router, doctors as doctors_router
        from app.routers import centers as centers_router, templates as templates_router
        from app.schemas import ReportCreate, DoctorCreate, CenterCreate, TemplateCreate
    except ImportError:
        from app import case_ops
        from app.routers import reports as reports_router, doctors as doctors_router
        from app.routers import centers as centers_router, templates as templates_router
        from app.schemas import ReportCreate, DoctorCreate, CenterCreate, TemplateCreate
    from pydantic import ValidationError

    at = (appr.action_type or "").upper()
    et = (appr.entity_type or "").lower()
    eid = appr.entity_id or p.get("id")

    if et == "case" or "CASE" in at:
        if at.startswith("DELETE"):
            cid = case_ops.resolve_case_id(db, eid) if eid else None
            if cid:
                case_ops.delete_case_cascade(db, cid)
            return {"event": "CASE_DELETED", "caseId": cid or eid}
        if at.startswith("UPDATE"):
            case = db.query(CaseDB).filter(CaseDB.id == eid).first() if eid else None
            if not case:
                raise HTTPException(status_code=409, detail="This patient record no longer exists")
            try:
                details = case_ops.clean_case_details(p)
            except ValueError as ve:
                raise HTTPException(status_code=400, detail=str(ve))
            before, changes, added = case_ops.apply_case_details(db, case, details, p.get("addImages"))
            note = case_ops.describe_changes(before, changes, added)
            if note:
                case_ops.add_edit_note(
                    db, case, reviewer,
                    f"{note} (requested by {appr.manager_name or appr.manager_id}, approved by {reviewer.name})",
                )
            return {"event": "CASE_UPDATED", "caseId": case.id}
        if at.startswith("CREATE"):
            # Same path as a Super Admin / centre upload: one study per body part,
            # doctor alerts, sequence numbers for billing.
            try:
                report_in = ReportCreate(**p)
            except ValidationError:
                report_in = None
            if report_in is not None:
                saved = await reports_router.save_report_core(report_in, db)
                return {"event": "NEW_REPORT", "caseId": saved.get("id") if isinstance(saved, dict) else None}
        _apply_case_action(appr.action_type, appr.entity_id, p, db)
        return {"event": "NEW_REPORT", "caseId": eid}

    if et == "doctor" or "DOCTOR" in at:
        if at.startswith("DELETE"):
            doctors_router.delete_doctor_core(db, eid)
            return {"event": "DOCTOR_DELETED", "doctorId": eid}
        if at.startswith("UPDATE"):
            if not db.query(DoctorDB).filter(DoctorDB.id == eid).first():
                raise HTTPException(status_code=409, detail="This doctor no longer exists")
            doctors_router.save_doctor_core(DoctorCreate(**{**p, "id": eid}), db)
            return {"event": "DOCTOR_UPDATED", "doctorId": eid}
        _apply_doctor_action(appr.action_type, appr.entity_id, p, db)
        return {"event": "DOCTOR_CREATED"}

    if et == "center" or "CENTER" in at:
        if at.startswith("DELETE"):
            centers_router.delete_center_core(db, eid)
            return {"event": "CENTER_DELETED", "centerId": eid}
        if at.startswith("UPDATE"):
            if not db.query(CenterDB).filter(CenterDB.id == eid).first():
                raise HTTPException(status_code=409, detail="This centre no longer exists")
            centers_router.save_center_core(CenterCreate(**{**p, "id": eid}), db)
            return {"event": "CENTER_UPDATED", "centerId": eid}
        _apply_center_action(appr.action_type, appr.entity_id, p, db)
        return {"event": "CENTER_CREATED"}

    if et == "template" or "TEMPLATE" in at:
        if at.startswith("DELETE"):
            templates_router.delete_template_core(db, eid)
            return {"event": "TEMPLATE_DELETED", "templateId": eid}
        if at.startswith("UPDATE"):
            if not db.query(TemplateDB).filter(TemplateDB.id == eid).first():
                raise HTTPException(status_code=409, detail="This template no longer exists")
            templates_router.save_template_core(TemplateCreate(**{**p, "id": eid}), db)
            return {"event": "TEMPLATE_UPDATED", "templateId": eid}
        _apply_template_action(appr.action_type, appr.entity_id, p, db)
        return {"event": "TEMPLATE_CREATED"}

    raise HTTPException(status_code=400, detail=f"Unknown approval type {appr.action_type}")


@router.post("/{approval_id}/approve", response_model=ApprovalResponse)
async def approve_request(
    approval_id: str,
    req: Optional[ApprovalReviewRequest] = None,
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    appr = db.query(PendingApprovalDB).filter(PendingApprovalDB.id == approval_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval request not found")
    
    if appr.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Approval request is already {appr.status}")
    
    payload = appr.payload or {}
    reviewer_name = (req.reviewerName if req and req.reviewerName else None) or _user.name or "Super Admin"

    # Remember the case's doctor so an approved edit that moves it can notify the new doctor
    is_case_action = appr.entity_type == "case" or "CASE" in appr.action_type
    claim_before = None
    if is_case_action and appr.entity_id:
        st_before = db.query(StudyDB).filter(StudyDB.case_id == appr.entity_id).first()
        claim_before = st_before.claimed_by if st_before else None
    
    # Apply staged action to actual database tables
    try:
        applied = await _apply_approved(appr, payload, db, _user)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to apply approved change: {str(e)}")
    
    appr.status = "APPROVED"
    appr.reviewed_at = datetime.utcnow().isoformat()
    appr.reviewed_by = reviewer_name
    
    db.commit()
    db.refresh(appr)
    
    schema_data = db_to_schema(appr)
    
    # Broadcast WS resolution
    await ws_manager.broadcast({
        "type": "APPROVAL_RESOLVED",
        "approvalId": appr.id,
        "status": "APPROVED",
        "actionType": appr.action_type,
        "entityType": appr.entity_type,
        "entityId": appr.entity_id,
        "reviewedBy": reviewer_name,
        "approval": schema_data
    })
    if applied.get("event") in ("CASE_DELETED", "CASE_UPDATED") and applied.get("caseId"):
        await ws_manager.broadcast({"type": applied["event"], "reportId": applied["caseId"], "caseId": applied["caseId"]})

    if is_case_action and appr.entity_id and claim_before and applied.get("event") != "CASE_DELETED":
        st_after = db.query(StudyDB).filter(StudyDB.case_id == appr.entity_id).first()
        claim_after = st_after.claimed_by if st_after else None
        case_row = db.query(CaseDB).filter(CaseDB.id == appr.entity_id).first()
        if claim_after and claim_after != claim_before and case_row:
            await notif_svc.notify_case_reassigned(db, case_row, claim_after, _user)

    await notif_svc.notify_approval_resolved(db, appr, _user)
    return schema_data

@router.post("/{approval_id}/reject", response_model=ApprovalResponse)
async def reject_request(
    approval_id: str,
    req: ApprovalReviewRequest,
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    appr = db.query(PendingApprovalDB).filter(PendingApprovalDB.id == approval_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval request not found")
    
    if appr.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Approval request is already {appr.status}")
    
    reviewer_name = req.reviewerName or _user.name or "Super Admin"
    reason = req.rejectionReason or "Rejected by Super Admin"
    
    appr.status = "REJECTED"
    appr.rejection_reason = reason
    appr.reviewed_at = datetime.utcnow().isoformat()
    appr.reviewed_by = reviewer_name
    
    db.commit()
    db.refresh(appr)
    
    schema_data = db_to_schema(appr)
    
    # Broadcast WS resolution
    await ws_manager.broadcast({
        "type": "APPROVAL_RESOLVED",
        "approvalId": appr.id,
        "status": "REJECTED",
        "rejectionReason": reason,
        "reviewedBy": reviewer_name,
        "approval": schema_data
    })

    await notif_svc.notify_approval_resolved(db, appr, _user)
    return schema_data



def _resolve_case_modality(p: dict) -> str:
    raw = p.get("modality")
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return pricing.DEFAULT_STUDY_MODALITY  # legacy fallback when payload omits modality
    value = str(raw).strip()
    aliases = {
        "xray": "X-Ray", "x-ray": "X-Ray", "x ray": "X-Ray",
        "ct": "CT", "ct scan": "CT", "mri": "MRI",
        "sonography": "Sonography", "ultrasound": "Sonography", "usg": "Sonography",
        "blood report": "Blood Report", "blood": "Blood Report",
    }
    key = value.lower()
    if key in aliases:
        value = aliases[key]
    if value not in pricing.ALLOWED_STUDY_MODALITIES:
        value = pricing.DEFAULT_STUDY_MODALITY
    return value

def _apply_case_action(action_type: str, entity_id: Optional[str], p: dict, db: Session):
    case_id = entity_id or p.get("id") or f"case-{int(time.time() * 1000)}"
    if action_type.startswith("DELETE"):
        try:
            from app import case_ops
        except ImportError:
            from app import case_ops
        case_ops.delete_case_cascade(db, case_ops.resolve_case_id(db, case_id) or case_id)
        return

    # Create or update case
    c = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    body_parts = p.get("bodyParts", ["X-Ray"])
    body_part_str = ", ".join(body_parts) if isinstance(body_parts, list) else str(body_parts)
    now_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    is_urg = bool(p.get("isUrgent", False))
    try:
        from app import storage as file_storage
    except ImportError:
        from app import storage as file_storage

    uploaded_images = file_storage.materialize_media_list(
        p.get("uploadedImages", []),
        category="cases",
        entity_id=case_id,
        subfolder="uploads",
    )
    dicom_snapshots = file_storage.materialize_media_list(
        p.get("dicomSnapshots", []),
        category="cases",
        entity_id=case_id,
        subfolder="snapshots",
    )
    dicom_file_url = file_storage.materialize_media_reference(
        p.get("dicomFileUrl"),
        category="cases",
        entity_id=case_id,
        subfolder="dicom",
        filename_hint="study.dcm",
    )

    meta = {
        "bodyParts": body_parts,
        "clinicalNotes": p.get("clinicalNotes"),
        "findings": p.get("findings"),
        "impression": p.get("impression"),
        "dicomFileUrl": dicom_file_url,
        "dicomMetadata": p.get("dicomMetadata"),
        "dicomSnapshots": dicom_snapshots or [],
        "uploadedImages": uploaded_images or [],
        "reportsByBodyPart": p.get("reportsByBodyPart", {}),
        "impressionsByBodyPart": p.get("impressionsByBodyPart", {}),
        "hasHeaderUrl": p.get("hasHeaderUrl", True),
        "hasNoHeaderUrl": p.get("hasNoHeaderUrl", True),
        "signatureApplied": p.get("signatureApplied", False),
        "isUrgent": is_urg,
        "modality": _resolve_case_modality(p),
    }

    if not c:
        c = CaseDB(
            id=case_id,
            patient_number=p.get("patientNumber", f"P-{int(time.time())}"),
            full_name=p.get("fullName", "Unnamed Patient"),
            age=int(p.get("age", 30)),
            gender=p.get("gender", "Male"),
            phone=p.get("phone", "0000000000"),
            radiology_center_id=p.get("radiologyCenterId", "center-1"),
            radiology_center_name=p.get("radiologyCenterName", "Main Center"),
            referring_physician_id=p.get("referringPhysicianId", "doc-ref-1"),
            referring_physician_name=p.get("referringPhysicianName", "Dr. Referring"),
            status=p.get("status", "Pending"),
            is_urgent=is_urg,
            study_date=p.get("studyDate", now_str),
            created_at=p.get("createdAt", now_str),
            metadata_=meta,
        )
        db.add(c)
        
        case_modality = _resolve_case_modality(p)
        st = StudyDB(
            id=case_id,
            case_id=case_id,
            modality=case_modality,
            body_part=body_part_str,
            status=p.get("claimStatus", "UNCLAIMED"),
            is_urgent=is_urg,
            clinical_notes=p.get("clinicalNotes"),
            findings=p.get("findings"),
            impression=p.get("impression"),
            doc_content=p.get("docContent"),
            claimed_by=p.get("claimedByDoctorId"),
            claimed_by_name=p.get("claimedByDoctorName"),
            sequence_no=1,  # single-study case: first (and only) study
            created_at=c.created_at,
            metadata_={},
        )
        db.add(st)

        rep = ReportDB(
            id=case_id,
            patient_number=c.patient_number,
            full_name=c.full_name,
            age=c.age,
            gender=c.gender,
            phone=c.phone,
            radiology_center_id=c.radiology_center_id,
            radiology_center_name=c.radiology_center_name,
            referring_physician_id=c.referring_physician_id,
            referring_physician_name=c.referring_physician_name,
            assigned_doctor_id=p.get("assignedDoctorId"),
            assigned_doctor_name=p.get("assignedDoctorName"),
            status=c.status,
            is_urgent=is_urg,
            study_date=c.study_date,
            created_at=c.created_at,
            metadata_=meta,
        )
        db.add(rep)
    else:
        c.full_name = p.get("fullName", c.full_name)
        c.age = int(p.get("age", c.age))
        c.gender = p.get("gender", c.gender)
        c.phone = p.get("phone", c.phone)
        c.status = p.get("status", c.status)
        c.is_urgent = is_urg
        c.study_date = p.get("studyDate", c.study_date)
        c.metadata_ = meta
        
        st = db.query(StudyDB).filter(StudyDB.case_id == case_id).first()
        if st:
            st.body_part = body_part_str
            st.is_urgent = is_urg
            st.clinical_notes = p.get("clinicalNotes", st.clinical_notes)
            st.findings = p.get("findings", st.findings)
            st.impression = p.get("impression", st.impression)
            st.doc_content = p.get("docContent", st.doc_content)
            st.claimed_by = p.get("claimedByDoctorId", st.claimed_by)
            st.claimed_by_name = p.get("claimedByDoctorName", st.claimed_by_name)
            if p.get("claimStatus"):
                st.status = p["claimStatus"]

        rep = db.query(ReportDB).filter(ReportDB.id == case_id).first()
        if rep:
            rep.full_name = c.full_name
            rep.age = c.age
            rep.gender = c.gender
            rep.phone = c.phone
            rep.status = c.status
            rep.is_urgent = is_urg
            rep.study_date = c.study_date
            rep.metadata_ = meta


    # Save images
    images = (uploaded_images or []) + (dicom_snapshots or [])
    if images:
        for idx, img_url in enumerate(images):
            img_id = f"img-{case_id}-{idx}"
            ex_img = db.query(StudyImageDB).filter(StudyImageDB.id == img_id).first()
            if not ex_img:
                db.add(StudyImageDB(
                    id=img_id,
                    study_id=case_id,
                    image_url=img_url,
                    image_type="uploaded",
                    created_at=now_str
                ))


def _apply_doctor_action(action_type: str, entity_id: Optional[str], p: dict, db: Session):
    doc_id = entity_id or p.get("id") or f"doc-{int(time.time() * 1000)}"
    if action_type.startswith("DELETE"):
        db.query(DoctorDB).filter(DoctorDB.id == doc_id).delete()
        return

    doc = db.query(DoctorDB).filter(DoctorDB.id == doc_id).first()
    try:
        from app import storage as file_storage
    except ImportError:
        from app import storage as file_storage

    meta = {
        "password": p.get("password"),
        "address": p.get("address"),
        "signatureUrl": file_storage.materialize_media_reference(
            p.get("signatureUrl"),
            category="doctors",
            entity_id=doc_id,
            subfolder="signature",
            # unique per upload so earlier signed reports keep their image
            filename_hint=f"signature-{uuid.uuid4().hex[:12]}",
        ),
        "profileFileUrl": file_storage.materialize_media_reference(
            p.get("profileFileUrl"),
            category="doctors",
            entity_id=doc_id,
            subfolder="profile",
            filename_hint="profile.png",
        ),
    }
    now_str = datetime.utcnow().strftime("%Y-%m-%d")

    if doc:
        doc.first_name = p.get("firstName", doc.first_name)
        doc.last_name = p.get("lastName", doc.last_name)
        doc.full_name = p.get("fullName", doc.full_name)
        doc.email = p.get("email", doc.email)
        doc.username = p.get("username", doc.username)
        doc.contact_number = p.get("contactNumber", doc.contact_number)
        doc.metadata_ = meta
    else:
        doc = DoctorDB(
            id=doc_id,
            first_name=p.get("firstName", "Doctor"),
            last_name=p.get("lastName", "Name"),
            full_name=p.get("fullName", "Dr. Doctor Name"),
            email=p.get("email", "doctor@example.com"),
            username=p.get("username"),
            contact_number=p.get("contactNumber", "0000000000"),
            created_at=now_str,
            metadata_=meta,
        )
        db.add(doc)

    raw_pw = p.get("password")
    hashed_pw = None
    if raw_pw:
        hashed_pw = raw_pw if looks_like_bcrypt(raw_pw) else hash_password(raw_pw)

    login_email = doc.username or doc.email
    if login_email and hashed_pw:
        ex_u = db.query(UserDB).filter(UserDB.email.ilike(login_email)).first()
        if ex_u:
            user_meta = dict(ex_u.metadata_ or {})
            user_meta["password"] = hashed_pw
            user_meta["doctorId"] = doc.id
            ex_u.name = doc.full_name
            ex_u.metadata_ = user_meta
            flag_modified(ex_u, "metadata_")
        else:
            db.add(UserDB(
                email=login_email,
                name=doc.full_name,
                role="DOCTOR",
                metadata_={"password": hashed_pw, "doctorId": doc.id}
            ))


def _apply_center_action(action_type: str, entity_id: Optional[str], p: dict, db: Session):
    center_id = entity_id or p.get("id") or f"center-{int(time.time() * 1000)}"
    if action_type.startswith("DELETE"):
        db.query(CenterDB).filter(CenterDB.id == center_id).delete()
        return

    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    try:
        from app import storage as file_storage
    except ImportError:
        from app import storage as file_storage

    meta = {
        "firstName": p.get("firstName"),
        "lastName": p.get("lastName"),
        "username": p.get("username"),
        "password": p.get("password"),
        "address": p.get("address"),
        "headerTemplateUrl": file_storage.materialize_media_reference(
            p.get("headerTemplateUrl"),
            category="centers",
            entity_id=center_id,
            subfolder="header",
            filename_hint="header.png",
        ),
        "logoUrl": file_storage.materialize_media_reference(
            p.get("logoUrl"),
            category="centers",
            entity_id=center_id,
            subfolder="logo",
            filename_hint="logo.png",
        ),
    }
    now_str = datetime.utcnow().strftime("%Y-%m-%d")

    if c:
        c.center_name = p.get("centerName", c.center_name)
        c.email = p.get("email", c.email)
        c.contact_number = p.get("contactNumber", c.contact_number)
        c.metadata_ = meta
    else:
        c = CenterDB(
            id=center_id,
            center_name=p.get("centerName", "New Center"),
            email=p.get("email", "center@example.com"),
            contact_number=p.get("contactNumber", "0000000000"),
            created_at=now_str,
            metadata_=meta,
        )
        db.add(c)

    login_email = p.get("username") or c.email
    raw_pw = p.get("password")
    if login_email and raw_pw:
        hashed_pw = raw_pw if looks_like_bcrypt(raw_pw) else hash_password(raw_pw)
        meta["password"] = hashed_pw
        c.metadata_ = meta
        ex_u = db.query(UserDB).filter(UserDB.email.ilike(login_email)).first()
        if ex_u:
            ex_u.name = c.center_name
            user_meta = dict(ex_u.metadata_ or {})
            user_meta["password"] = hashed_pw
            user_meta["centerId"] = c.id
            ex_u.metadata_ = user_meta
            flag_modified(ex_u, "metadata_")
        else:
            ex_u = UserDB(
                email=login_email,
                name=c.center_name,
                role="CENTER",
                metadata_={"password": hashed_pw, "centerId": c.id}
            )
            db.add(ex_u)
        db.flush()
        try:
            from app import access as access_svc
        except ImportError:
            from app import access as access_svc
        access_svc.ensure_primary_center_admin(db, ex_u, c.id, created_by="approval")


def _apply_template_action(action_type: str, entity_id: Optional[str], p: dict, db: Session):
    tmpl_id = entity_id or p.get("id") or f"tmpl-{int(time.time() * 1000)}"
    if action_type.startswith("DELETE"):
        db.query(TemplateDB).filter(TemplateDB.id == tmpl_id).delete()
        return

    t = db.query(TemplateDB).filter(TemplateDB.id == tmpl_id).first()
    meta = {
        "bodyPart": p.get("bodyPart"),
        "findings": p.get("findings"),
        "impression": p.get("impression"),
        "content": p.get("content"),
    }
    now_str = datetime.utcnow().strftime("%Y-%m-%d")

    if t:
        t.title = p.get("title", t.title)
        t.center_id = p.get("centerId", t.center_id)
        t.center_name = p.get("centerName", t.center_name)
        t.modality = p.get("modality", t.modality)
        t.metadata_ = meta
    else:
        t = TemplateDB(
            id=tmpl_id,
            title=p.get("title", "New Template"),
            center_id=p.get("centerId", "ALL"),
            center_name=p.get("centerName", "All Centers"),
            modality=p.get("modality", "X-Ray"),
            created_at=now_str,
            metadata_=meta,
        )
        db.add(t)
