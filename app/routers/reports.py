import hashlib
import time
import uuid
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import update, and_
from sqlalchemy.orm import Session
try:
    from app.database import get_db
    from app.models import (
        ReportDB, CaseDB, StudyDB, StudyImageDB, StudyNotificationDB, DoctorDB, UserDB, CenterDB
    )
    from app.schemas import ReportCreate, ReportResponse, RejectReportRequest
    from app.websocket import manager
    from app.security import get_current_user, require_roles
    from app import pricing
    from app import billing as billing_svc
    from app import access as access_svc
    from app import case_ops
    from app import notifications as notif_svc
except ImportError:
    from app.database import get_db
    from app.models import (
        ReportDB, CaseDB, StudyDB, StudyImageDB, StudyNotificationDB, DoctorDB, UserDB, CenterDB
    )
    from app.schemas import ReportCreate, ReportResponse, RejectReportRequest
    from app.websocket import manager
    from app.security import get_current_user, require_roles
    from app import pricing
    from app import billing as billing_svc
    from app import access as access_svc
    from app import case_ops
    from app import notifications as notif_svc
try:
    from app import storage as file_storage
except ImportError:
    from app import storage as file_storage

router = APIRouter(
    prefix="/reports",
    tags=["Reports"],
    dependencies=[Depends(get_current_user)],
)

class ClaimReportRequest(BaseModel):
    doctorId: str
    doctorName: str


def resolve_study_modality(raw) -> str:
    """Accept modality from request; validate against allowed set.
    Last-resort default X-Ray only for legacy callers that omit modality.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Legacy callers: default documented fallback
        return pricing.DEFAULT_STUDY_MODALITY
    value = str(raw).strip()
    # Normalize common aliases
    aliases = {
        "xray": "X-Ray",
        "x-ray": "X-Ray",
        "x ray": "X-Ray",
        "ct": "CT",
        "ct scan": "CT",
        "mri": "MRI",
        "sonography": "Sonography",
        "ultrasound": "Sonography",
        "usg": "Sonography",
        "blood report": "Blood Report",
        "blood": "Blood Report",
    }
    key = value.lower()
    if key in aliases:
        value = aliases[key]
    if value not in pricing.ALLOWED_STUDY_MODALITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid modality '{raw}'. Allowed: {', '.join(pricing.ALLOWED_STUDY_MODALITIES)}",
        )
    return value


def db_to_schema(r: ReportDB) -> dict:
    meta = r.metadata_ or {}
    return {
        "id": r.id,
        "patientNumber": r.patient_number,
        "fullName": r.full_name,
        "age": r.age,
        "ageUnit": meta.get("ageUnit", "Years"),
        "gender": r.gender,
        "phone": r.phone,
        "radiologyCenterId": r.radiology_center_id,
        "radiologyCenterName": r.radiology_center_name,
        "bodyParts": meta.get("bodyParts", []),
        "modality": meta.get("modality", pricing.DEFAULT_STUDY_MODALITY),
        "referringPhysicianId": r.referring_physician_id,
        "referringPhysicianName": r.referring_physician_name,
        "assignedDoctorId": r.assigned_doctor_id,
        "assignedDoctorName": r.assigned_doctor_name,
        "assignedDoctorDegree": meta.get("assignedDoctorDegree", "M.D. (Radiodiagnosis)"),
        "assignedDoctorRegNo": meta.get("assignedDoctorRegNo") or None,  # no made-up default
        "assignedDoctorIds": meta.get("assignedDoctorIds", ["ALL"]),
        "claimedByDoctorId": meta.get("claimedByDoctorId"),
        "claimedByDoctorName": meta.get("claimedByDoctorName"),
        "claimStatus": meta.get("claimStatus", "UNCLAIMED"),
        "status": r.status,
        "isUrgent": bool(r.is_urgent) if r.is_urgent is not None else meta.get("isUrgent", False),
        "isPortable": meta.get("isPortable", False),
        "studyDate": r.study_date,
        "clinicalNotes": meta.get("clinicalNotes"),
        "findings": meta.get("findings"),
        "impression": meta.get("impression"),
        "reportsByBodyPart": meta.get("reportsByBodyPart"),
        "impressionsByBodyPart": meta.get("impressionsByBodyPart"),
        "dicomFileUrl": file_storage.public_url(meta.get("dicomFileUrl")),
        "dicomMetadata": meta.get("dicomMetadata"),
        "docContent": meta.get("docContent"),
        "dicomSnapshots": file_storage.public_url_list(meta.get("dicomSnapshots")),
        "uploadedImages": file_storage.public_url_list(meta.get("uploadedImages")),
        "clinicalHistoryImages": file_storage.public_url_list(meta.get("clinicalHistoryImages")),
        "hasHeaderUrl": meta.get("hasHeaderUrl", True),
        "hasNoHeaderUrl": meta.get("hasNoHeaderUrl", True),
        "createdAt": r.created_at,
        "signatureApplied": meta.get("signatureApplied", False),
    }

def study_to_schema(study: StudyDB, case: Optional[CaseDB] = None) -> dict:
    meta = (study.metadata_ or {}) if study else {}
    is_urg = False
    if study and study.is_urgent is not None:
        is_urg = bool(study.is_urgent)
    elif case and case.is_urgent is not None:
        is_urg = bool(case.is_urgent)
    else:
        is_urg = meta.get("isUrgent", False)

    return {
        "id": study.id if study else "rep-unknown",
        "patientNumber": case.patient_number if case else "PAT-0000",
        "fullName": case.full_name if case else "Unnamed Patient",
        "age": case.age if case else 30,
        "ageUnit": meta.get("ageUnit", "Years"),
        "gender": case.gender if case else "Male",
        "phone": case.phone if case else "0000000000",
        "radiologyCenterId": case.radiology_center_id if case else "center-1",
        "radiologyCenterName": case.radiology_center_name if case else "CENTER",
        "bodyParts": [study.body_part] if study else [],
        "modality": (study.modality if study else None) or meta.get("modality", pricing.DEFAULT_STUDY_MODALITY),
        "referringPhysicianId": case.referring_physician_id if case else "ref-1",
        "referringPhysicianName": case.referring_physician_name if case else "DR. REFERRING",
        "assignedDoctorId": study.claimed_by if study else None,
        "assignedDoctorName": study.claimed_by_name if study else None,
        "assignedDoctorDegree": meta.get("assignedDoctorDegree", "M.D. (Radiodiagnosis)"),
        "assignedDoctorRegNo": meta.get("assignedDoctorRegNo") or None,  # no made-up default
        "assignedDoctorIds": meta.get("assignedDoctorIds", ["ALL"]),
        "claimedByDoctorId": study.claimed_by if study else None,
        "claimedByDoctorName": study.claimed_by_name if study else None,
        "claimStatus": "CLAIMED" if (study and study.status == "CLAIMED") else "UNCLAIMED",
        "status": study.status if study else "In Review",
        "isUrgent": is_urg,
        "isPortable": meta.get("isPortable", False),
        "studyDate": case.study_date if case else "14/09/2026",
        "clinicalNotes": study.clinical_notes if study else None,
        "findings": study.findings if study else None,
        "impression": study.impression if study else None,
        "reportsByBodyPart": meta.get("reportsByBodyPart"),
        "impressionsByBodyPart": meta.get("impressionsByBodyPart"),
        "dicomFileUrl": file_storage.public_url(meta.get("dicomFileUrl")),
        "dicomMetadata": meta.get("dicomMetadata"),
        "docContent": study.doc_content if study else None,
        "dicomSnapshots": file_storage.public_url_list(meta.get("dicomSnapshots")),
        "uploadedImages": file_storage.public_url_list(meta.get("uploadedImages")),
        "clinicalHistoryImages": file_storage.public_url_list(meta.get("clinicalHistoryImages")),
        "hasHeaderUrl": meta.get("hasHeaderUrl", True),
        "hasNoHeaderUrl": meta.get("hasNoHeaderUrl", True),
        "createdAt": case.created_at if case else "2026-09-14T00:00:00Z",
        "signatureApplied": meta.get("signatureApplied", False),
    }

def _doctor_claim_identity_keys(db: Session, user: UserDB) -> set:
    """Map JWT doctor user to claim identity forms used historically.

    StudyDB.claimed_by stores doctor id (preferred); some legacy rows stored
    email/username. Identity never comes from request params.
    """
    meta = user.metadata_ or {}
    doctor_id = meta.get("doctorId")
    if not doctor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Doctor account missing doctorId",
        )
    keys = {str(doctor_id)}
    if user.email:
        keys.add(user.email)
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if doc:
        if doc.email:
            keys.add(doc.email)
        if doc.username:
            keys.add(doc.username)
    return keys


def _parse_work_date(raw: Optional[str]):
    """Parse case.study_date (DD/MM/YYYY or DD-MM-YYYY) or ISO date/datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            piece = s[:10] if len(s) >= 10 else s
            return datetime.strptime(piece, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_my_work_status_filter(raw: Optional[str]) -> Optional[str]:
    """Map filter to completed | in_progress using existing status values only."""
    if raw is None or str(raw).strip() == "":
        return None
    v = str(raw).strip()
    low = v.lower().replace("_", " ").replace("-", " ")
    if v == "Completed" or low == "completed":
        return "completed"
    # Existing in-progress values: CLAIMED (StudyDB) and In Review (Case/Report)
    if v in ("CLAIMED", "In Review") or low in ("claimed", "in review", "in progress"):
        return "in_progress"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid status filter. Use CLAIMED, In Review, or Completed.",
    )


def _case_work_item(db: Session, case: CaseDB, studies: list) -> dict:
    """Build My Work list item (case-level; workspace opens by case/report id).

    Built from CaseDB + StudyDB only. Avoids ReportDB ORM here because the live
    Postgres xray_reports table is missing columns present on the model (e.g.
    is_urgent), which would 500 the list ? same drift already affects GET /reports.
    """
    _ = db  # reserved if future safe legacy merge is added
    primary = studies[0] if studies else None
    if primary:
        item = study_to_schema(primary, case)
        item["bodyParts"] = [s.body_part for s in studies if s.body_part]
        item["claimedByDoctorId"] = primary.claimed_by
        item["claimedByDoctorName"] = primary.claimed_by_name
        item["assignedDoctorId"] = primary.claimed_by
        item["assignedDoctorName"] = primary.claimed_by_name
        item["claimStatus"] = "CLAIMED"
    else:
        item = {
            "id": case.id,
            "patientNumber": case.patient_number,
            "fullName": case.full_name,
            "age": case.age,
            "ageUnit": "Years",
            "gender": case.gender,
            "phone": case.phone,
            "radiologyCenterId": case.radiology_center_id,
            "radiologyCenterName": case.radiology_center_name,
            "bodyParts": [],
            "modality": pricing.DEFAULT_STUDY_MODALITY,
            "referringPhysicianId": case.referring_physician_id,
            "referringPhysicianName": case.referring_physician_name,
            "assignedDoctorId": None,
            "assignedDoctorName": None,
            "claimedByDoctorId": None,
            "claimedByDoctorName": None,
            "claimStatus": "CLAIMED",
            "status": case.status,
            "isUrgent": bool(case.is_urgent) if case.is_urgent is not None else False,
            "studyDate": case.study_date,
            "createdAt": case.created_at,
        }
    # Prefer case status: StudyDB often remains CLAIMED after sign-off
    if case.status:
        item["status"] = case.status
    # Workspace / list identity is the case id (not per-study id)
    item["id"] = case.id
    item["studyCount"] = len(studies)
    item["workDate"] = case.study_date
    try:
        from app.routers.study_reports import enrich_case_item
    except ImportError:
        from app.routers.study_reports import enrich_case_item
    item = enrich_case_item(item, case, studies)
    # Surface partial progress for My Work UI
    signed = item.get("signedStudyCount", 0)
    total = item.get("studyCount", len(studies))
    if item.get("isPartial"):
        item["progressLabel"] = f"{signed} of {total} reported"
    return item


@router.get("/my-work")
def get_my_work(
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    center_id: Optional[str] = Query(None, alias="center"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # Intentionally unused: doctor scope comes ONLY from JWT
    doctor_id: Optional[str] = Query(None, alias="doctorId"),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    """Paginated cases this doctor claimed (in progress) or completed.

    Date filter uses CaseDB.study_date (no claim/completion timestamp exists).
    Query param doctorId is ignored and cannot widen scope.
    """
    _ = doctor_id  # discarded — never used for scoping
    identity_keys = _doctor_claim_identity_keys(db, current_user)
    status_mode = _normalize_my_work_status_filter(status_filter)
    from_d = _parse_work_date(date_from) if date_from else None
    to_d = _parse_work_date(date_to) if date_to else None
    if date_from and from_d is None:
        raise HTTPException(status_code=400, detail="Invalid 'from' date; use YYYY-MM-DD")
    if date_to and to_d is None:
        raise HTTPException(status_code=400, detail="Invalid 'to' date; use YYYY-MM-DD")

    studies = (
        db.query(StudyDB)
        .filter(StudyDB.claimed_by.in_(list(identity_keys)))
        .all()
    )
    by_case = {}
    for s in studies:
        by_case.setdefault(s.case_id, []).append(s)

    empty = {
        "items": [],
        "total": 0,
        "page": page,
        "page_size": page_size,
        "dateField": "cases.study_date",
        "centers": [],
    }
    if not by_case:
        return empty

    cases = db.query(CaseDB).filter(CaseDB.id.in_(list(by_case.keys()))).all()
    case_map = {c.id: c for c in cases}

    items_meta = []
    for case_id, study_list in by_case.items():
        case = case_map.get(case_id)
        if not case:
            continue
        if center_id and case.radiology_center_id != center_id:
            continue
        is_completed = (case.status or "") == "Completed"
        if status_mode == "completed" and not is_completed:
            continue
        if status_mode == "in_progress" and is_completed:
            continue
        work_d = _parse_work_date(case.study_date)
        if work_d is None:
            work_d = _parse_work_date(case.created_at)
        if from_d and work_d and work_d < from_d:
            continue
        if to_d and work_d and work_d > to_d:
            continue
        if (from_d or to_d) and work_d is None:
            continue
        items_meta.append((case, study_list, work_d, case.created_at or ""))

    def _meta_sort_key(t):
        case, st_list, _wd, ca = t
        try:
            from app.routers.study_reports import is_partial_case
        except ImportError:
            from app.routers.study_reports import is_partial_case
        urgent = 0 if bool(case.is_urgent) else 1
        partial = 0 if is_partial_case(st_list) else 1
        # created_at desc within group via inverse string works poorly; bucket below
        return (urgent, partial)

    buckets = {}
    for t in items_meta:
        key = _meta_sort_key(t)
        buckets.setdefault(key, []).append(t)
    items_meta = []
    for key in sorted(buckets.keys()):
        group = buckets[key]
        group.sort(key=lambda t: t[3] or "", reverse=True)
        items_meta.extend(group)

    total = len(items_meta)
    start = (page - 1) * page_size
    page_rows = items_meta[start : start + page_size]
    items = [_case_work_item(db, case, st_list) for case, st_list, _wd, _ca in page_rows]

    centers_in_work = sorted(
        {
            (c.radiology_center_id, c.radiology_center_name)
            for c, _s, _wd, _ca in items_meta
            if c.radiology_center_id
        },
        key=lambda x: (x[1] or x[0]),
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "dateField": "cases.study_date",
        "centers": [{"id": cid, "name": cname} for cid, cname in centers_in_work],
    }


@router.get("")
def get_reports(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    if current_user.role == "CENTER":
        # Every linked centre where this login has Reports >= read
        center_ids = access_svc.center_ids_with(db, current_user, "reports", "read") or []
        if not center_ids:
            return []
        reports = (
            db.query(ReportDB)
            .filter(ReportDB.radiology_center_id.in_(center_ids))
            .order_by(ReportDB.created_at.desc())
            .all()
        )
    elif current_user.role == "DOCTOR":
        identity_keys = _doctor_claim_identity_keys(db, current_user)
        all_reports = db.query(ReportDB).order_by(ReportDB.created_at.desc()).all()
        reports = []
        for r in all_reports:
            meta = r.metadata_ or {}
            claimed_by = meta.get("claimedByDoctorId")
            assigned_ids = meta.get("assignedDoctorIds", ["ALL"])
            if claimed_by and str(claimed_by) in identity_keys:
                reports.append(r)
            elif not claimed_by and ("ALL" in assigned_ids or any(str(k) in assigned_ids for k in identity_keys)):
                reports.append(r)
    else:
        # SUPER_ADMIN, MANAGER
        reports = db.query(ReportDB).order_by(ReportDB.created_at.desc()).all()

    try:
        from app.routers.study_reports import (
            case_studies_ordered, enrich_case_item, worklist_sort_key,
        )
    except ImportError:
        from app.routers.study_reports import (
            case_studies_ordered, enrich_case_item, worklist_sort_key,
        )
    res = []
    for r in reports:
        case = db.query(CaseDB).filter(CaseDB.id == r.id).first()
        studies = case_studies_ordered(db, r.id) if case else []
        item = db_to_schema(r)
        if case:
            item = enrich_case_item(item, case, studies)
        else:
            item["isPartial"] = False
        res.append(item)
    res.sort(key=worklist_sort_key)
    return res

@router.get("/{report_id}")
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    try:
        from app.routers.study_reports import case_studies_ordered, enrich_case_item
        from app.routers.case_thread import _can_access_case
    except ImportError:
        from app.routers.study_reports import case_studies_ordered, enrich_case_item
        from app.routers.case_thread import _can_access_case

    case = db.query(CaseDB).filter(CaseDB.id == report_id).first()
    if not case:
        study = db.query(StudyDB).filter(StudyDB.id == report_id).first()
        if study:
            case = db.query(CaseDB).filter(CaseDB.id == study.case_id).first()

    if case and not _can_access_case(db, current_user, case):
        raise HTTPException(status_code=403, detail="Access denied to patient report")

    report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if not report:
        study = db.query(StudyDB).filter(StudyDB.id == report_id).first()
        if not study:
            raise HTTPException(status_code=404, detail="Report not found")
        c = db.query(CaseDB).filter(CaseDB.id == study.case_id).first()
        item = study_to_schema(study, c)
        if c:
            studies = case_studies_ordered(db, c.id)
            item = enrich_case_item(item, c, studies)
        return item
    c = db.query(CaseDB).filter(CaseDB.id == report.id).first()
    item = db_to_schema(report)
    if c:
        studies = case_studies_ordered(db, c.id)
        item = enrich_case_item(item, c, studies)
    return item

@router.post("", response_model=ReportResponse)
async def save_report(
    report_in: ReportCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Create a case (upload) or save the legacy whole-report payload.
    * Super Admin: anything.
    * Centre login: Reports = write on the target centre (and on the existing case's centre).
    * Doctor: only cases they can already open (legacy review/sign); cannot create cases.
    * Manager: a new case is staged for Super Admin approval (202); editing goes through
      PATCH /reports/{id} (also approval-gated)."""
    existing_case = db.query(CaseDB).filter(CaseDB.id == report_in.id).first() if report_in.id else None
    existing_report = None
    if report_in.id and existing_case is None:
        existing_report = db.query(ReportDB).filter(ReportDB.id == report_in.id).first()
    is_new = existing_case is None and existing_report is None
    role = current_user.role
    if role == "CENTER":
        access_svc.require_center_perm(db, current_user, report_in.radiologyCenterId, "reports", "write")
        old_center = existing_case.radiology_center_id if existing_case else (
            existing_report.radiology_center_id if existing_report else None)
        if old_center and old_center != report_in.radiologyCenterId:
            access_svc.require_center_perm(db, current_user, old_center, "reports", "write")
    elif role == "DOCTOR":
        if is_new:
            raise HTTPException(status_code=403, detail="Doctors cannot create patient records")
        try:
            from app.routers.case_thread import _can_access_case
        except ImportError:
            from app.routers.case_thread import _can_access_case
        if existing_case is not None and not _can_access_case(db, current_user, existing_case):
            raise HTTPException(status_code=403, detail="Access denied to patient report")
    elif role == "MANAGER":
        if not is_new:
            raise HTTPException(status_code=403, detail="Managers edit patient records with the Edit action (Super Admin approval)")
        try:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        except ImportError:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        payload = report_in.model_dump() if hasattr(report_in, "model_dump") else report_in.dict()
        appr = create_pending_approval(
            db, current_user, action_type="CREATE_CASE", entity_type="case", entity_id=None, payload=payload,
        )
        data = await announce_pending_approval(db, appr, current_user)
        return pending_response(data, "New patient record sent to the Super Admin for approval.")
    elif role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return await save_report_core(report_in, db)


async def save_report_core(report_in: ReportCreate, db: Session):
    # The centre's name comes from the centres table, not from the client (it is printed on invoices).
    if report_in.radiologyCenterId:
        _center = db.query(CenterDB).filter(CenterDB.id == report_in.radiologyCenterId).first()
        if _center is not None and _center.center_name:
            report_in.radiologyCenterName = _center.center_name
    rep_id = report_in.id or str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat() + "Z"
    
    is_urg = bool(report_in.isUrgent) if report_in.isUrgent is not None else False
    study_modality = resolve_study_modality(getattr(report_in, "modality", None))

    # Capture prior completion state for idempotent billing trigger
    prior_report = db.query(ReportDB).filter(ReportDB.id == rep_id).first()
    prior_case = db.query(CaseDB).filter(CaseDB.id == rep_id).first()
    was_completed = False
    if prior_report and prior_report.status == "Completed":
        was_completed = True
    if prior_case and prior_case.status == "Completed":
        was_completed = True

    uploaded_images = file_storage.materialize_media_list(
        report_in.uploadedImages,
        category="cases",
        entity_id=rep_id,
        subfolder="uploads",
    )
    clinical_history_images = file_storage.materialize_media_list(
        getattr(report_in, "clinicalHistoryImages", None),
        category="cases",
        entity_id=rep_id,
        subfolder="clinical",
    )
    dicom_snapshots = file_storage.materialize_media_list(
        report_in.dicomSnapshots,
        category="cases",
        entity_id=rep_id,
        subfolder="snapshots",
    )
    dicom_file_url = file_storage.materialize_media_reference(
        report_in.dicomFileUrl,
        category="cases",
        entity_id=rep_id,
        subfolder="dicom",
        filename_hint="study.dcm",
    )

    meta_payload = {
        "bodyParts": report_in.bodyParts,
        "ageUnit": report_in.ageUnit or "Years",
        "clinicalNotes": report_in.clinicalNotes,
        "findings": report_in.findings,
        "impression": report_in.impression,
        "reportsByBodyPart": report_in.reportsByBodyPart,
        "impressionsByBodyPart": report_in.impressionsByBodyPart,
        "dicomFileUrl": dicom_file_url,
        "dicomMetadata": report_in.dicomMetadata,
        "docContent": report_in.docContent,
        "dicomSnapshots": dicom_snapshots,
        "uploadedImages": uploaded_images,
        "clinicalHistoryImages": clinical_history_images,
        "assignedDoctorIds": report_in.assignedDoctorIds or ["ALL"],
        "assignedDoctorDegree": report_in.assignedDoctorDegree or "M.D. (Radiodiagnosis)",
        "assignedDoctorRegNo": report_in.assignedDoctorRegNo or None,
        "claimedByDoctorId": report_in.claimedByDoctorId,
        "claimedByDoctorName": report_in.claimedByDoctorName,
        "claimStatus": report_in.claimStatus or "UNCLAIMED",
        "hasHeaderUrl": report_in.hasHeaderUrl if report_in.hasHeaderUrl is not None else True,
        "hasNoHeaderUrl": report_in.hasNoHeaderUrl if report_in.hasNoHeaderUrl is not None else True,
        "signatureApplied": report_in.signatureApplied if report_in.signatureApplied is not None else False,
        "isUrgent": is_urg,
        "isPortable": bool(report_in.isPortable) if report_in.isPortable is not None else False,
        "modality": study_modality,
    }

    # 1. Save or update Cases table
    case_obj = db.query(CaseDB).filter(CaseDB.id == rep_id).first()
    if not case_obj:
        case_obj = CaseDB(
            id=rep_id,
            patient_number=report_in.patientNumber,
            full_name=report_in.fullName,
            age=report_in.age,
            gender=report_in.gender,
            phone=report_in.phone,
            radiology_center_id=report_in.radiologyCenterId,
            radiology_center_name=report_in.radiologyCenterName,
            referring_physician_id=report_in.referringPhysicianId,
            referring_physician_name=report_in.referringPhysicianName,
            status=report_in.status,
            is_urgent=is_urg,
            study_date=report_in.studyDate,
            created_at=created_at,
            metadata_=meta_payload,
        )
        db.add(case_obj)
    else:
        case_obj.patient_number = report_in.patientNumber
        case_obj.full_name = report_in.fullName
        case_obj.age = report_in.age
        case_obj.gender = report_in.gender
        case_obj.phone = report_in.phone
        case_obj.radiology_center_id = report_in.radiologyCenterId
        case_obj.radiology_center_name = report_in.radiologyCenterName
        case_obj.referring_physician_id = report_in.referringPhysicianId
        case_obj.referring_physician_name = report_in.referringPhysicianName
        case_obj.status = report_in.status
        case_obj.is_urgent = is_urg
        case_obj.study_date = report_in.studyDate
        case_obj.metadata_ = meta_payload

    db.commit()

    # 2. Split bodyParts array into individual studies rows
    body_parts = report_in.bodyParts if report_in.bodyParts else ["CHEST PA/AP"]
    all_doctors = db.query(DoctorDB).all()
    notified_doc_ids = [d.id for d in all_doctors] if "ALL" in (report_in.assignedDoctorIds or ["ALL"]) else (report_in.assignedDoctorIds or [])

    # Study sequence (billing position): a brand-new case numbers its studies by
    # their 1-based position in this request's bodyParts. A study added later to
    # an existing case gets max(existing sequence_no)+1. Existing studies are
    # never renumbered. Legacy cases whose studies have no sequence_no keep NULL
    # (billing then falls back to the old created_at, id ordering).
    existing_seqs = [
        r[0] for r in db.query(StudyDB.sequence_no).filter(StudyDB.case_id == rep_id).all()
    ]
    is_new_case = not existing_seqs
    next_seq = (
        max(existing_seqs) + 1
        if existing_seqs and all(v is not None for v in existing_seqs)
        else None
    )

    for position, bp in enumerate(body_parts, start=1):
        sanitized_bp = bp.replace('/', '-').replace(' ', '-').lower()
        study_id = f"{rep_id}-{sanitized_bp}"
        if len(study_id) > 100:
            # studies.id is VARCHAR(100): keep the case prefix, hash an over-long body-part name
            study_id = f"{rep_id}-{hashlib.sha1(sanitized_bp.encode('utf-8')).hexdigest()[:16]}"
        study_obj = db.query(StudyDB).filter(StudyDB.id == study_id).first()
        if not study_obj:
            if is_new_case:
                seq_no = position
            elif next_seq is not None:
                seq_no = next_seq
                next_seq += 1
            else:
                seq_no = None
            study_obj = StudyDB(
                id=study_id,
                case_id=rep_id,
                modality=study_modality,
                body_part=bp,
                status="UNCLAIMED",
                is_urgent=is_urg,
                clinical_notes=report_in.clinicalNotes,
                findings=report_in.findings,
                impression=report_in.impression,
                doc_content=report_in.docContent,
                claimed_by=report_in.claimedByDoctorId,
                claimed_by_name=report_in.claimedByDoctorName,
                sequence_no=seq_no,
                created_at=created_at,
                metadata_=meta_payload,
            )
            db.add(study_obj)
            db.commit()

            # Insert uploaded images into study_images
            if uploaded_images:
                for idx, img_url in enumerate(uploaded_images):
                    img_id = billing_svc._bounded_id(f"{study_id}-img-{idx}", "img")
                    image_obj = StudyImageDB(
                        id=img_id,
                        study_id=study_id,
                        image_url=img_url,
                        image_type="uploaded",
                        created_at=created_at,
                    )
                    db.add(image_obj)
                db.commit()

            # Populate study_notifications table
            for doc_id in notified_doc_ids:
                notif_obj = StudyNotificationDB(
                    study_id=study_id,
                    doctor_id=doc_id,
                    notified_at=created_at,
                    dismissed_reason=None
                )
                db.add(notif_obj)
            db.commit()
        else:
            study_obj.is_urgent = is_urg
            study_obj.modality = study_modality
            db.commit()

    # 3. Save to legacy ReportDB table for backward compatibility
    existing_report = db.query(ReportDB).filter(ReportDB.id == rep_id).first()
    if existing_report:
        existing_report.patient_number = report_in.patientNumber
        existing_report.full_name = report_in.fullName
        existing_report.age = report_in.age
        existing_report.gender = report_in.gender
        existing_report.phone = report_in.phone
        existing_report.radiology_center_id = report_in.radiologyCenterId
        existing_report.radiology_center_name = report_in.radiologyCenterName
        existing_report.referring_physician_id = report_in.referringPhysicianId
        existing_report.referring_physician_name = report_in.referringPhysicianName
        existing_report.assigned_doctor_id = report_in.assignedDoctorId
        existing_report.assigned_doctor_name = report_in.assignedDoctorName
        existing_report.status = report_in.status
        existing_report.is_urgent = is_urg
        existing_report.study_date = report_in.studyDate
        existing_report.metadata_ = meta_payload
        db.commit()
        db.refresh(existing_report)
        result = db_to_schema(existing_report)
    else:
        new_report = ReportDB(
            id=rep_id,
            patient_number=report_in.patientNumber,
            full_name=report_in.fullName,
            age=report_in.age,
            gender=report_in.gender,
            phone=report_in.phone,
            radiology_center_id=report_in.radiologyCenterId,
            radiology_center_name=report_in.radiologyCenterName,
            referring_physician_id=report_in.referringPhysicianId,
            referring_physician_name=report_in.referringPhysicianName,
            assigned_doctor_id=report_in.assignedDoctorId,
            assigned_doctor_name=report_in.assignedDoctorName,
            status=report_in.status,
            is_urgent=is_urg,
            study_date=report_in.studyDate,
            created_at=created_at,
            metadata_=meta_payload,
        )
        db.add(new_report)
        db.commit()
        db.refresh(new_report)
        result = db_to_schema(new_report)

    # Per-study rule: never mark case Completed / notify center / bill until ALL studies SIGNED.
    try:
        from app.routers.study_reports import (
            case_studies_ordered, enrich_case_item, signed_count, finalize_case_if_complete,
            notify_case_completed,
        )
    except ImportError:
        from app.routers.study_reports import (
            case_studies_ordered, enrich_case_item, signed_count, finalize_case_if_complete,
            notify_case_completed,
        )

    case_obj = db.query(CaseDB).filter(CaseDB.id == rep_id).first()
    studies = case_studies_ordered(db, rep_id) if case_obj else []
    if case_obj and studies:
        result = enrich_case_item(result, case_obj, studies)

    if report_in.status == "Completed" and case_obj and studies:
        signed, total = signed_count(studies)
        if signed < total:
            # Mid-case: keep In Review, no center notification, no billing
            case_obj.status = "In Review"
            report_row = db.query(ReportDB).filter(ReportDB.id == rep_id).first()
            if report_row:
                report_row.status = "In Review"
            db.commit()
            if report_row:
                result = enrich_case_item(db_to_schema(report_row), case_obj, studies)
            await manager.broadcast({"type": "NEW_REPORT", "report": result})
            return result
        # All studies signed (or single-study legacy complete) → finalize once
        finalize = await finalize_case_if_complete(db, case_obj, studies)
        db.commit()
        # Completion is announced only after billing succeeded and the commit went through.
        await notify_case_completed(finalize)
        report_row = db.query(ReportDB).filter(ReportDB.id == rep_id).first()
        if report_row:
            result = enrich_case_item(db_to_schema(report_row), case_obj, studies)
        if finalize.get("billing"):
            result = dict(result)
            result["billing"] = finalize["billing"]
        return result

    # Legacy Completed with no studies row: keep prior billing path for safety.
    # The broadcast below now runs AFTER this billing attempt, so a failed
    # billing (409 raised, or a logged error) never announces REPORT_COMPLETED.
    legacy_billing_failed = False
    if report_in.status == "Completed" and not studies and not billing_svc.case_already_billed(db, rep_id):
        try:
            billing_summary = billing_svc.create_billing_on_sign_off(db, case_id=rep_id)
            db.commit()
            if billing_summary:
                result = dict(result)
                result["billing"] = billing_summary
        except ValueError as be:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(be))
        except Exception as be:
            db.rollback()
            import logging
            logging.getLogger(__name__).exception("Billing on sign-off failed for %s: %s", rep_id, be)
            legacy_billing_failed = True

    if not (report_in.status == "Completed" and legacy_billing_failed):
        await manager.broadcast({
            "type": "NEW_REPORT" if report_in.status != "Completed" else "REPORT_COMPLETED",
            "report": result
        })
    return result

@router.post("/{report_id}/claim", response_model=ReportResponse)
async def claim_report(
    report_id: str,
    claim_req: ClaimReportRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    doc_id = (current_user.metadata_ or {}).get("doctorId")
    if not doc_id:
        raise HTTPException(status_code=403, detail="Doctor account missing doctorId profile")
    doctor_row = db.query(DoctorDB).filter(DoctorDB.id == doc_id).first()
    doctor_name = doctor_row.full_name if doctor_row else current_user.name

    # Override request payload identity with authenticated doctor user
    claim_req.doctorId = str(doc_id)
    claim_req.doctorName = doctor_name

    # ATOMIC CONDITIONAL UPDATE: UPDATE studies SET claimed_by = :doctor_id, status = 'CLAIMED' WHERE id = :id AND status = 'UNCLAIMED'
    # Checked strictly via result.rowcount (NO PRIOR SELECT!)
    stmt = (
        update(StudyDB)
        .where(and_((StudyDB.id == report_id) | (StudyDB.case_id == report_id), StudyDB.status == "UNCLAIMED"))
        .values(
            claimed_by=claim_req.doctorId,
            claimed_by_name=claim_req.doctorName,
            status="CLAIMED"
        )
    )
    result = db.execute(stmt)
    db.commit()

    if result.rowcount == 0:
        # Atomic conditional update affected 0 rows because study is already claimed or legacy ID
        current_study = db.query(StudyDB).filter((StudyDB.id == report_id) | (StudyDB.case_id == report_id)).first()
        if current_study and current_study.status == "CLAIMED":
            claimant = current_study.claimed_by_name or "another doctor"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Study already claimed by {claimant}."
            )

        # Legacy ReportDB check if report_id was a legacy case ID.
        # Row lock (SELECT ... FOR UPDATE) so two doctors cannot both pass the claim check below;
        # it is held until the commit after this doctor's claim is written into the metadata.
        legacy_report = db.query(ReportDB).filter(ReportDB.id == report_id).with_for_update().first()
        if not legacy_report:
            raise HTTPException(status_code=404, detail="Study not found")

        meta = legacy_report.metadata_ or {}
        if meta.get("claimStatus") == "CLAIMED" and meta.get("claimedByDoctorId") != claim_req.doctorId:
            claimant = meta.get("claimedByDoctorName", "another doctor")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Study already claimed by {claimant}."
            )

        # Atomic conditional update on ReportDB
        legacy_stmt = (
            update(ReportDB)
            .where(and_(ReportDB.id == report_id, ReportDB.status != "Completed"))
            .values(
                assigned_doctor_id=claim_req.doctorId,
                assigned_doctor_name=claim_req.doctorName,
                status="In Review"
            )
        )
        legacy_res = db.execute(legacy_stmt)
        if legacy_res.rowcount:
            # Record the claim while the row lock is still held (same values as the sync below)
            legacy_meta = dict(legacy_report.metadata_ or {})
            legacy_meta["claimedByDoctorId"] = claim_req.doctorId
            legacy_meta["claimedByDoctorName"] = claim_req.doctorName
            legacy_meta["claimStatus"] = "CLAIMED"
            legacy_report.metadata_ = legacy_meta
        db.commit()

    # Single Source of Truth: Update study_notifications join table
    db.query(StudyNotificationDB).filter(
        and_((StudyNotificationDB.study_id == report_id), StudyNotificationDB.doctor_id == claim_req.doctorId)
    ).update({"dismissed_reason": "accepted_self"})

    db.query(StudyNotificationDB).filter(
        and_((StudyNotificationDB.study_id == report_id), StudyNotificationDB.doctor_id != claim_req.doctorId)
    ).update({"dismissed_reason": "accepted_other"})

    db.commit()

    # Sync legacy ReportDB metadata for UI compatibility
    report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if report:
        report.assigned_doctor_id = claim_req.doctorId
        report.assigned_doctor_name = claim_req.doctorName
        report.status = "In Review"
        meta = dict(report.metadata_ or {})
        meta["claimedByDoctorId"] = claim_req.doctorId
        meta["claimedByDoctorName"] = claim_req.doctorName
        meta["claimStatus"] = "CLAIMED"
        report.metadata_ = meta
        db.commit()
        db.refresh(report)
        updated_result = db_to_schema(report)

    else:
        study = db.query(StudyDB).filter((StudyDB.id == report_id) | (StudyDB.case_id == report_id)).first()
        case = db.query(CaseDB).filter(CaseDB.id == study.case_id).first() if study else None
        updated_result = study_to_schema(study, case)

    # Broadcast real-time REPORT_CLAIMED event via WebSocket to all doctors
    await manager.broadcast({
        "type": "REPORT_CLAIMED",
        "reportId": report_id,
        "claimedByDoctorId": claim_req.doctorId,
        "claimedByDoctorName": claim_req.doctorName,
        "report": updated_result
    })

    return updated_result

@router.post("/{report_id}/reject")
async def reject_report(report_id: str, req: RejectReportRequest, db: Session = Depends(get_db)):
    # Update study_notifications join table setting dismissed_reason = 'rejected' for doctor
    db.query(StudyNotificationDB).filter(
        and_((StudyNotificationDB.study_id == report_id), StudyNotificationDB.doctor_id == req.doctorId)
    ).update({"dismissed_reason": req.reason or "rejected"})
    db.commit()

    # Broadcast notification rejection event so frontend components sync state if needed
    await manager.broadcast({
        "type": "REPORT_REJECTED",
        "reportId": report_id,
        "doctorId": req.doctorId,
    })

    return {"status": "success", "message": f"Report {report_id} notification rejected by doctor {req.doctorId}"}

class CaseDetailsUpdate(BaseModel):
    fullName: Optional[str] = None
    age: Optional[int] = None
    ageUnit: Optional[str] = None
    gender: Optional[str] = None
    phone: Optional[str] = None
    referringPhysicianName: Optional[str] = None
    clinicalNotes: Optional[str] = None
    isUrgent: Optional[bool] = None
    isPortable: Optional[bool] = None
    # Extra scan images (stored file paths from /files/upload, or data: URLs)
    addImages: Optional[List[str]] = None


def _report_item(db: Session, case_id: str) -> Optional[dict]:
    try:
        from app.routers.study_reports import case_studies_ordered, enrich_case_item
    except ImportError:
        from app.routers.study_reports import case_studies_ordered, enrich_case_item
    rep = db.query(ReportDB).filter(ReportDB.id == case_id).first()
    case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    if not rep:
        return None
    item = db_to_schema(rep)
    if case:
        item = enrich_case_item(item, case, case_studies_ordered(db, case_id))
    return item


@router.patch("/{report_id}")
async def update_case_details(
    report_id: str,
    body: CaseDetailsUpdate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Edit a patient record's details (row Edit action).
    * Super Admin: applied at once.
    * Centre login: applied at once with Reports = write for the case's centre.
    * Manager: staged as UPDATE_CASE for Super Admin approval (202).
    * Doctor / others: 403.
    Centre, modality and body parts are not editable (they drive studies and billing).
    Every applied edit is written to the case's activity thread (Chat)."""
    case_id = case_ops.resolve_case_id(db, report_id)
    case = db.query(CaseDB).filter(CaseDB.id == case_id).first() if case_id else None
    if not case:
        raise HTTPException(status_code=404, detail="Patient record not found")
    role = current_user.role
    if role == "CENTER":
        access_svc.require_center_perm(db, current_user, case.radiology_center_id, "reports", "write")
    elif role not in ("SUPER_ADMIN", "MANAGER"):
        raise HTTPException(status_code=403, detail="You are not allowed to edit patient records")

    raw = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    try:
        details = case_ops.clean_case_details(raw)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    add_images = [i for i in (body.addImages or []) if isinstance(i, str) and i.strip()]
    before = case_ops.case_details_snapshot(case)
    changes = case_ops.changed_fields(before, details)
    if not changes and not add_images:
        return {"updated": False, "message": "No changes", "report": _report_item(db, case.id)}

    if role == "MANAGER":
        try:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        except ImportError:
            from app.routers.approvals import create_pending_approval, announce_pending_approval, pending_response
        staged_images = case_ops.store_added_images(case.id, add_images)
        payload = {**before, **details, "patientNumber": case.patient_number, "id": case.id}
        if staged_images:
            payload["addImages"] = staged_images
        appr = create_pending_approval(
            db, current_user, action_type="UPDATE_CASE", entity_type="case", entity_id=case.id, payload=payload,
        )
        data = await announce_pending_approval(db, appr, current_user)
        return pending_response(data, "Edit sent to the Super Admin for approval.")

    before, applied, added = case_ops.apply_case_details(db, case, details, add_images)
    case_ops.add_edit_note(db, case, current_user, case_ops.describe_changes(before, applied, added))
    db.commit()
    item = _report_item(db, case.id)
    await manager.broadcast({"type": "CASE_UPDATED", "reportId": case.id, "caseId": case.id, "report": item})
    return {"updated": True, "changes": applied, "addedImages": added, "report": item}


@router.delete("/{report_id}", status_code=204)
async def delete_report(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    """Only Super Admin and Manager may delete.
    * Super Admin: the case and all its dependent rows are removed at once
      (billing lines and files on disk are kept).
    * Manager: staged as DELETE_CASE for Super Admin approval (202)."""
    case_id = case_ops.resolve_case_id(db, report_id)
    if not case_id:
        raise HTTPException(status_code=404, detail="Patient record not found")
    if current_user.role == "MANAGER":
        try:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_case,
            )
        except ImportError:
            from app.routers.approvals import (
                create_pending_approval, announce_pending_approval, pending_response, _snapshot_case,
            )
        appr = create_pending_approval(
            db, current_user, action_type="DELETE_CASE", entity_type="case", entity_id=case_id,
            payload=_snapshot_case(case_id, db) or {"id": case_id},
        )
        data = await announce_pending_approval(db, appr, current_user)
        return pending_response(data, "Delete request sent to the Super Admin for approval.")
    case_ops.delete_case_cascade(db, case_id)
    db.commit()
    await manager.broadcast({"type": "CASE_DELETED", "reportId": case_id, "caseId": case_id})
    return None
