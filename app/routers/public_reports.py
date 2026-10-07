"""Public, read-only view of a signed study report, reached through its QR-code token.
No login. Only the content printed on the report is returned (no internal IDs)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

try:
    from app.database import get_db
    from app.models import CaseDB, CenterDB, ReportDB, StudyDB
    from app import storage as file_storage
    from app.report_links import find_active_link
    from app.routers.study_reports import signer_public, study_report_status
except ImportError:
    from app.database import get_db
    from app.models import CaseDB, CenterDB, ReportDB, StudyDB
    from app import storage as file_storage
    from app.report_links import find_active_link
    from app.routers.study_reports import signer_public, study_report_status

router = APIRouter(prefix="/public", tags=["Public reports"])

NOT_FOUND = "This report link is not valid or has been withdrawn."


@router.get("/reports/{token}")
def get_public_report(token: str, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    link = find_active_link(db, token)
    if link is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    study = db.query(StudyDB).filter(StudyDB.id == link.study_id).first()
    case = db.query(CaseDB).filter(CaseDB.id == link.case_id).first() if study else None
    if study is None or case is None or study_report_status(study) != "SIGNED":
        raise HTTPException(status_code=404, detail=NOT_FOUND)

    cmeta = case.metadata_ or {}
    rrow = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    rmeta = (rrow.metadata_ if rrow else None) or {}
    smeta = study.metadata_ or {}
    center = db.query(CenterDB).filter(CenterDB.id == case.radiology_center_id).first()
    centermeta = (center.metadata_ if center else None) or {}

    signer = dict(signer_public(study) or {})
    signer.pop("doctorId", None)  # nothing internal on the public view
    signer = signer or {
        "name": study.signed_by_name,
        "degree": None,
        "registrationNumber": None,
        "signatureUrl": None,
        "signedAt": study.signed_at,
    }
    modality = study.modality or cmeta.get("modality") or rmeta.get("modality") or "X-Ray"
    return {
        "center": {
            "name": (center.center_name if center else None) or case.radiology_center_name,
            "address": centermeta.get("address"),
            "phone": center.contact_number if center else None,
            "logoUrl": file_storage.public_url(centermeta.get("logoUrl")) if centermeta.get("logoUrl") else None,
            "headerTemplateUrl": file_storage.public_url(centermeta.get("headerTemplateUrl")) if centermeta.get("headerTemplateUrl") else None,
            "letterheadMode": centermeta.get("letterheadMode", "full-page"),
        },
        "patient": {
            "name": case.full_name,
            "patientId": case.patient_number,
            "age": case.age,
            "ageUnit": cmeta.get("ageUnit") or rmeta.get("ageUnit") or "Years",
            "gender": case.gender,
            "studyDate": case.study_date,
            "referringDoctor": case.referring_physician_name,
        },
        "study": {
            "modality": modality,
            "bodyPart": study.body_part,
            "title": smeta.get("title") or f"{str(modality).upper()} {study.body_part}".strip(),
            "technique": study.technique or f"{modality} - {study.body_part}",
            "findings": study.findings or "",
            "impression": study.impression or "",
            "clinicalHistory": study.clinical_notes or cmeta.get("clinicalNotes") or rmeta.get("clinicalNotes") or "",
            "signedAt": study.signed_at,
        },
        "signer": signer,
    }
