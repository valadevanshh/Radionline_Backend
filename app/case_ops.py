"""Case-level operations shared by the reports router and the approvals flow:
proper delete (every dependent row) and the patient-details edit."""
import logging
import uuid
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.models import (
        CaseDB, StudyDB, StudyImageDB, StudyNotificationDB, ReportDB, ReportCommentDB,
        ReportPublicLinkDB, ImageAnnotationDB, UserNotificationDB, UserDB,
    )
    from app import storage as file_storage
except ImportError:
    from app.models import (
        CaseDB, StudyDB, StudyImageDB, StudyNotificationDB, ReportDB, ReportCommentDB,
        ReportPublicLinkDB, ImageAnnotationDB, UserNotificationDB, UserDB,
    )
    from app import storage as file_storage

logger = logging.getLogger(__name__)

# Patient details a centre / admin may correct after upload. Centre, modality and
# body parts are NOT editable here: they drive the per-study rows and billing.
EDITABLE_CASE_FIELDS = (
    "fullName", "age", "ageUnit", "gender", "phone",
    "referringPhysicianName", "clinicalNotes", "isUrgent", "isPortable",
)
FIELD_LABELS = {
    "fullName": "Name", "age": "Age", "ageUnit": "Age unit", "gender": "Gender", "phone": "Phone",
    "referringPhysicianName": "Referring doctor", "clinicalNotes": "Clinical notes",
    "isUrgent": "Urgent", "isPortable": "Portable",
}
GENDERS = ("Male", "Female", "Other")
AGE_UNITS = ("Years", "Months", "Days")


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def resolve_case_id(db: Session, any_id: str) -> Optional[str]:
    """Case id for a case id, a legacy report id or a study id."""
    if db.query(CaseDB.id).filter(CaseDB.id == any_id).first():
        return any_id
    st = db.query(StudyDB).filter(StudyDB.id == any_id).first()
    if st:
        return st.case_id
    if db.query(ReportDB.id).filter(ReportDB.id == any_id).first():
        return any_id
    return None


def delete_case_cascade(db: Session, case_id: str) -> dict:
    """Delete a patient case and everything that hangs off it. Does NOT commit.

    Removed: study images, doctor alerts (study_notifications), studies, the case,
    the legacy report row, case activity thread, public QR links, PACS annotations
    and bell notifications pointing at the case.
    Kept on purpose: invoice line items (billing history is immutable) and the
    uploaded files on disk."""
    study_ids = [r[0] for r in db.query(StudyDB.id).filter(StudyDB.case_id == case_id).all()]
    counts = {}
    img_keys = study_ids + [case_id]  # legacy images were keyed by the case id
    counts["study_images"] = db.query(StudyImageDB).filter(StudyImageDB.study_id.in_(img_keys)).delete(synchronize_session=False)
    if study_ids:
        counts["study_notifications"] = db.query(StudyNotificationDB).filter(
            StudyNotificationDB.study_id.in_(study_ids)).delete(synchronize_session=False)
    else:
        counts["study_notifications"] = 0
    counts["report_comments"] = db.query(ReportCommentDB).filter(ReportCommentDB.case_id == case_id).delete(synchronize_session=False)
    counts["report_public_links"] = db.query(ReportPublicLinkDB).filter(ReportPublicLinkDB.case_id == case_id).delete(synchronize_session=False)
    counts["image_annotations"] = db.query(ImageAnnotationDB).filter(ImageAnnotationDB.case_id == case_id).delete(synchronize_session=False)
    counts["user_notifications"] = db.query(UserNotificationDB).filter(UserNotificationDB.case_id == case_id).delete(synchronize_session=False)
    counts["studies"] = db.query(StudyDB).filter(StudyDB.case_id == case_id).delete(synchronize_session=False)
    counts["cases"] = db.query(CaseDB).filter(CaseDB.id == case_id).delete(synchronize_session=False)
    counts["xray_reports"] = db.query(ReportDB).filter(ReportDB.id == case_id).delete(synchronize_session=False)
    return counts


def case_details_snapshot(case: CaseDB) -> dict:
    meta = case.metadata_ or {}
    return {
        "fullName": case.full_name,
        "age": case.age,
        "ageUnit": meta.get("ageUnit") or "Years",
        "gender": case.gender,
        "phone": case.phone,
        "referringPhysicianName": case.referring_physician_name,
        "clinicalNotes": meta.get("clinicalNotes"),
        "isUrgent": bool(case.is_urgent) if case.is_urgent is not None else bool(meta.get("isUrgent")),
        "isPortable": bool(meta.get("isPortable")),
    }


def clean_case_details(raw: dict) -> dict:
    """Validate / normalise an edit request. Only EDITABLE_CASE_FIELDS that were sent."""
    out = {}
    for k in EDITABLE_CASE_FIELDS:
        if k not in raw or raw[k] is None:
            continue
        v = raw[k]
        if k == "fullName":
            v = " ".join(str(v).split()).upper()
            if not v:
                raise ValueError("Patient name is required")
            v = v[:255]
        elif k == "age":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("Age must be a whole number")
            if v < 0 or v > 150:
                raise ValueError("Age must be between 0 and 150")
        elif k == "ageUnit":
            v = str(v).strip().title()
            if v not in AGE_UNITS:
                raise ValueError("Age unit must be Years, Months or Days")
        elif k == "gender":
            v = str(v).strip().title()
            if v not in GENDERS:
                raise ValueError("Gender must be Male, Female or Other")
        elif k == "phone":
            v = str(v).strip()[:50]
        elif k == "referringPhysicianName":
            v = " ".join(str(v).split()).upper()[:255]
        elif k == "clinicalNotes":
            v = str(v)
        elif k in ("isUrgent", "isPortable"):
            v = bool(v)
        out[k] = v
    return out


def changed_fields(before: dict, details: dict) -> dict:
    return {k: v for k, v in details.items() if before.get(k) != v}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or v == "":
        return "-"
    s = " ".join(str(v).split())
    return s if len(s) <= 60 else s[:57] + "..."


def describe_changes(before: dict, changes: dict, added_images: int = 0) -> str:
    parts = [f"{FIELD_LABELS.get(k, k)}: {_fmt(before.get(k))} -> {_fmt(v)}" for k, v in changes.items()]
    if added_images:
        parts.append(f"{added_images} image(s) added")
    return "; ".join(parts)


def store_added_images(case_id: str, images: Optional[list]) -> list:
    """Store extra scan images for an existing case. Every data: URL gets a unique file
    name (the upload-time names uploads-1, uploads-2 ... must never be overwritten);
    already-stored paths are kept as they are."""
    out = []
    for img in images or []:
        if not isinstance(img, str) or not img.strip():
            continue
        stored = file_storage.materialize_media_reference(
            img.strip(), category="cases", entity_id=case_id, subfolder="uploads",
            filename_hint=f"added-{uuid.uuid4().hex[:16]}",
        )
        if stored and stored not in out:
            out.append(stored)
    return out


def apply_case_details(db: Session, case: CaseDB, details: dict, add_images: Optional[list] = None) -> Tuple[dict, dict, int]:
    """Write the patient-details edit onto the case, its studies and the legacy report row.
    Returns (before, changes, number_of_images_added). Does NOT commit."""
    before = case_details_snapshot(case)
    changes = changed_fields(before, details)
    stored_images = store_added_images(case.id, add_images)

    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    studies = db.query(StudyDB).filter(StudyDB.case_id == case.id).all()

    def _apply_meta(meta: dict) -> dict:
        meta = dict(meta or {})
        if "ageUnit" in changes:
            meta["ageUnit"] = changes["ageUnit"]
        if "clinicalNotes" in changes:
            meta["clinicalNotes"] = changes["clinicalNotes"]
        if "isUrgent" in changes:
            meta["isUrgent"] = changes["isUrgent"]
        if "isPortable" in changes:
            meta["isPortable"] = changes["isPortable"]
        if stored_images:
            ups = list(meta.get("uploadedImages") or [])
            snaps = list(meta.get("dicomSnapshots") or [])
            for img in stored_images:
                if img not in ups:
                    ups.append(img)
                if img not in snaps:
                    snaps.append(img)
            meta["uploadedImages"] = ups
            meta["dicomSnapshots"] = snaps
        return meta

    for row in [case] + ([report] if report else []):
        if "fullName" in changes:
            row.full_name = changes["fullName"]
        if "age" in changes:
            row.age = changes["age"]
        if "gender" in changes:
            row.gender = changes["gender"]
        if "phone" in changes:
            row.phone = changes["phone"]
        if "referringPhysicianName" in changes:
            row.referring_physician_name = changes["referringPhysicianName"]
        if "isUrgent" in changes:
            row.is_urgent = changes["isUrgent"]
        row.metadata_ = _apply_meta(row.metadata_)
        flag_modified(row, "metadata_")

    now = _now_iso()
    for st in studies:
        if "isUrgent" in changes:
            st.is_urgent = changes["isUrgent"]
        if "clinicalNotes" in changes and (st.report_status or "PENDING") != "SIGNED":
            st.clinical_notes = changes["clinicalNotes"]
        if stored_images:
            existing = {r[0] for r in db.query(StudyImageDB.image_url).filter(StudyImageDB.study_id == st.id).all()}
            for img in stored_images:
                if img in existing:
                    continue
                db.add(StudyImageDB(
                    id=f"img-{uuid.uuid4().hex}",
                    study_id=st.id, image_url=img, image_type="uploaded", created_at=now,
                ))
    return before, changes, len(stored_images)


def add_edit_note(db: Session, case: CaseDB, user: Optional[UserDB], text: str) -> None:
    """Audit line in the case's activity thread (shown in Chat). Does NOT commit."""
    if not text:
        return
    studies = db.query(StudyDB.id).filter(StudyDB.case_id == case.id).first()
    db.add(ReportCommentDB(
        case_id=case.id,
        study_id=studies[0] if studies else None,
        author_user_id=user.id if user is not None else None,
        author_role=(user.role if user is not None else "SYSTEM") or "SYSTEM",
        author_name=(user.name if user is not None else "System") or "System",
        kind="EDIT",
        body=f"Patient details updated. {text}"[:4000],
        created_at=_now_iso(),
    ))
