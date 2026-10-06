"""Per-study report sign-off / draft / leave-guard endpoints."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from app.database import get_db, engine
    from app.models import (
        ReportDB, CaseDB, StudyDB, DoctorDB, UserDB
    )
    from app.security import get_current_user, require_roles
    from app.websocket import manager
    from app import billing as billing_svc
    from app.routers.reports import (
        db_to_schema,
        study_to_schema,
        _doctor_claim_identity_keys,
    )
except ImportError:
    from app.database import get_db, engine
    from app.models import (
        ReportDB, CaseDB, StudyDB, DoctorDB, UserDB
    )
    from app.security import get_current_user, require_roles
    from app.websocket import manager
    from app import billing as billing_svc
    from app.routers.reports import (
        db_to_schema,
        study_to_schema,
        _doctor_claim_identity_keys,
    )

try:
    from app import storage as file_storage
    from app import report_links
except ImportError:
    from app import storage as file_storage
    from app import report_links

logger = logging.getLogger(__name__)


def usable_signature_path(value) -> Optional[str]:
    """A stored/served signature image, or None (placeholder images are not a signature)."""
    raw = str(value or "").strip()
    if not raw or "placehold.co" in raw or raw.startswith("data:"):
        return None
    return raw


def build_signer_snapshot(doc, doctor_id, doctor_name: str, signed_at: str) -> dict:
    """Doctor details frozen onto a study report at signing time, so later profile
    edits do not change reports that are already signed."""
    meta = (doc.metadata_ or {}) if doc is not None else {}
    return {
        "doctorId": str(doctor_id) if doctor_id else None,
        "name": (doc.full_name if doc is not None else None) or doctor_name,
        "degree": (str(meta.get("degree") or "").strip() or None),
        "registrationNumber": (str(meta.get("registrationNumber") or "").strip() or None),
        "signaturePath": usable_signature_path(meta.get("signatureUrl")),
        "signedAt": signed_at,
    }


def _public_link_fields(study) -> dict:
    token = (study.metadata_ or {}).get("publicToken") if study_report_status(study) == "SIGNED" else None
    return {"publicToken": token, "publicUrl": report_links.public_report_url(token)}


def signer_public(study) -> Optional[dict]:
    snap = (study.metadata_ or {}).get("signer") if study is not None else None
    if not isinstance(snap, dict):
        return None
    out = {k: snap.get(k) for k in ("doctorId", "name", "degree", "registrationNumber", "signedAt")}
    out["signatureUrl"] = file_storage.public_url(snap.get("signaturePath")) if snap.get("signaturePath") else None
    return out

router = APIRouter(
    prefix="/reports",
    tags=["Study Reports"],
    dependencies=[Depends(get_current_user)],
)


class StudyReportPayload(BaseModel):
    findings: Optional[str] = None
    impression: Optional[str] = None
    technique: Optional[str] = None
    templateId: Optional[str] = None
    clinicalNotes: Optional[str] = None
    docContent: Optional[str] = None
    dicomSnapshots: Optional[List[str]] = None
    # Report title shown on the page / PDF (saved on sign so the public view matches)
    title: Optional[str] = None


def ensure_study_report_columns(bind=None) -> None:
    """Additive: studies report fields + xray_reports.is_urgent (Postgres/SQLite)."""
    from sqlalchemy import text as sa_text, inspect as sa_inspect
    from sqlalchemy.orm import sessionmaker

    target = bind or engine
    dialect = target.dialect.name
    insp = sa_inspect(target)

    def cols(table: str) -> set:
        try:
            return {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return set()

    study_cols = {
        "report_status": "VARCHAR(20) DEFAULT 'PENDING'",
        "technique": "TEXT",
        "template_id": "VARCHAR(100)",
        "signed_at": "VARCHAR(50)",
        "signed_by": "VARCHAR(100)",
        "signed_by_name": "VARCHAR(255)",
        "sequence_no": "INTEGER",  # upload-order position for billing (nullable, no backfill)
    }
    # is_urgent may already exist on studies/cases; still ensure on xray_reports
    report_cols = {"is_urgent": "BOOLEAN DEFAULT false"}
    case_cols = {"is_urgent": "BOOLEAN DEFAULT false"}
    study_extra = {"is_urgent": "BOOLEAN DEFAULT false"}

    with target.begin() as conn:
        have = cols("studies")
        for col, ddl in {**study_cols, **study_extra}.items():
            if col in have:
                continue
            if dialect == "postgresql":
                conn.execute(sa_text(f"ALTER TABLE studies ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            else:
                # SQLite: IF NOT EXISTS for ADD COLUMN is not always available — guard via have
                conn.execute(sa_text(f"ALTER TABLE studies ADD COLUMN {col} {ddl}"))
            logger.info("Added studies.%s", col)

        have_r = cols("xray_reports")
        for col, ddl in report_cols.items():
            if col in have_r:
                continue
            if dialect == "postgresql":
                conn.execute(sa_text(f"ALTER TABLE xray_reports ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            else:
                conn.execute(sa_text(f"ALTER TABLE xray_reports ADD COLUMN {col} {ddl}"))
            logger.info("Added xray_reports.%s", col)

        have_c = cols("cases")
        for col, ddl in case_cols.items():
            if col in have_c:
                continue
            if dialect == "postgresql":
                conn.execute(sa_text(f"ALTER TABLE cases ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            else:
                conn.execute(sa_text(f"ALTER TABLE cases ADD COLUMN {col} {ddl}"))
            logger.info("Added cases.%s", col)

    # Migrate legacy content into per-study fields (additive)
    Session = sessionmaker(bind=target)
    db = Session()
    try:
        cases = db.query(CaseDB).all()
        for case in cases:
            studies = (
                db.query(StudyDB)
                .filter(StudyDB.case_id == case.id)
                .order_by(StudyDB.created_at.asc(), StudyDB.id.asc())
                .all()
            )
            if not studies:
                continue
            meta = case.metadata_ or {}
            rbp = meta.get("reportsByBodyPart") or {}
            ibp = meta.get("impressionsByBodyPart") or {}
            report_row = db.query(ReportDB).filter(ReportDB.id == case.id).first()
            rmeta = (report_row.metadata_ if report_row else None) or {}
            if not rbp:
                rbp = rmeta.get("reportsByBodyPart") or {}
            if not ibp:
                ibp = rmeta.get("impressionsByBodyPart") or {}
            case_done = (case.status or "") == "Completed"
            for st in studies:
                if (not st.findings) and st.body_part and st.body_part in rbp:
                    st.findings = rbp.get(st.body_part)
                if (not st.impression) and st.body_part and st.body_part in ibp:
                    st.impression = ibp.get(st.body_part)
                rs = (getattr(st, "report_status", None) or "").strip().upper()
                if not rs or rs == "PENDING":
                    if case_done or (st.status or "") == "Completed":
                        st.report_status = "SIGNED"
                        if not getattr(st, "signed_at", None):
                            st.signed_at = case.created_at
                        if not getattr(st, "signed_by", None) and st.claimed_by:
                            st.signed_by = st.claimed_by
                            st.signed_by_name = st.claimed_by_name
                    elif st.findings or st.impression:
                        st.report_status = "DRAFT"
                    else:
                        st.report_status = "PENDING"
                if not getattr(st, "technique", None):
                    st.technique = (
                        f"{st.modality or 'X-Ray'} - {st.body_part}"
                        if st.body_part
                        else (st.modality or "X-Ray")
                    )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Per-study migration skipped: %s", e)
    finally:
        db.close()


def study_report_status(study: StudyDB) -> str:
    raw = (getattr(study, "report_status", None) or "").strip().upper()
    if raw in ("PENDING", "DRAFT", "SIGNED"):
        return raw
    if (study.status or "") == "Completed":
        return "SIGNED"
    if study.findings or study.impression:
        return "DRAFT"
    return "PENDING"


def case_studies_ordered(db: Session, case_id: str) -> list:
    return (
        db.query(StudyDB)
        .filter(StudyDB.case_id == case_id)
        .order_by(StudyDB.created_at.asc(), StudyDB.id.asc())
        .all()
    )


def signed_count(studies: list) -> tuple:
    signed = sum(1 for s in studies if study_report_status(s) == "SIGNED")
    return signed, len(studies)


def is_partial_case(studies: list) -> bool:
    signed, total = signed_count(studies)
    return total > 0 and 0 < signed < total


def worklist_sort_key(item: dict) -> tuple:
    urgent = 0 if item.get("isUrgent") else 1
    partial = 0 if item.get("isPartial") else 1
    created = item.get("createdAt") or ""
    # createdAt desc within group: invert string by using reverse sort elsewhere; here ascending key
    # Use negative by sorting with reverse on a composite — callers sort ascending on this tuple,
    # so put created as descending via inverse: we use "" pad — better: return created as-is and sort with custom.
    return (urgent, partial, created)


def enrich_case_item(item: dict, case: CaseDB, studies: list) -> dict:
    signed, total = signed_count(studies)
    item["studyCount"] = total
    item["signedStudyCount"] = signed
    item["pendingStudyCount"] = max(0, total - signed)
    item["isPartial"] = bool(total > 0 and 0 < signed < total)
    item["studies"] = [
        {
            "id": s.id,
            "bodyPart": s.body_part,
            "modality": s.modality,
            "reportStatus": study_report_status(s),
            "status": s.status,
            "findings": s.findings,
            "impression": s.impression,
            "technique": getattr(s, "technique", None),
            "templateId": getattr(s, "template_id", None),
            "signedAt": getattr(s, "signed_at", None),
            "signedBy": getattr(s, "signed_by", None),
            "signedByName": getattr(s, "signed_by_name", None),
            "claimedBy": s.claimed_by,
            "claimedByName": s.claimed_by_name,
            "isUrgent": bool(s.is_urgent) if s.is_urgent is not None else bool(case.is_urgent),
            # Doctor details frozen at signing (None for studies signed before this existed)
            "signer": signer_public(s) if study_report_status(s) == "SIGNED" else None,
            # Public read-only link (QR code), created at signing
            **_public_link_fields(s),
        }
        for s in studies
    ]
    rbp, ibp, tbp = {}, {}, {}
    for s in studies:
        if not s.body_part:
            continue
        if s.findings:
            rbp[s.body_part] = s.findings
        if s.impression:
            ibp[s.body_part] = s.impression
        tech = getattr(s, "technique", None)
        if tech:
            tbp[s.body_part] = tech
    if rbp:
        item["reportsByBodyPart"] = rbp
    if ibp:
        item["impressionsByBodyPart"] = ibp
    if tbp:
        item["techniquesByBodyPart"] = tbp
    if case and case.is_urgent is not None:
        item["isUrgent"] = bool(case.is_urgent)
    elif any(s.is_urgent for s in studies if s.is_urgent is not None):
        item["isUrgent"] = True
    return item


def resolve_case_and_study(db: Session, case_id: str, study_id: str):
    case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    study = None
    if not case:
        study = db.query(StudyDB).filter(StudyDB.id == case_id).first()
        if study:
            case = db.query(CaseDB).filter(CaseDB.id == study.case_id).first()
            study_id = study.id
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if not study:
        study = (
            db.query(StudyDB)
            .filter(StudyDB.id == study_id, StudyDB.case_id == case.id)
            .first()
        )
    if not study:
        raise HTTPException(status_code=404, detail="Study not found for case")
    return case, study


def doctor_may_edit_study(db: Session, user: UserDB, study: StudyDB) -> None:
    if user.role in ("SUPER_ADMIN", "MANAGER"):
        return
    if user.role != "DOCTOR":
        raise HTTPException(status_code=403, detail="Only doctors can edit study reports")
    keys = {str(k) for k in _doctor_claim_identity_keys(db, user)}
    if not study.claimed_by or str(study.claimed_by) not in keys:
        raise HTTPException(status_code=403, detail="Study is not claimed by you")


def apply_study_payload(study: StudyDB, payload: StudyReportPayload) -> None:
    if payload.findings is not None:
        study.findings = payload.findings
    if payload.impression is not None:
        study.impression = payload.impression
    if payload.technique is not None:
        study.technique = payload.technique
    if payload.templateId is not None:
        study.template_id = payload.templateId
    if payload.clinicalNotes is not None:
        study.clinical_notes = payload.clinicalNotes
    if payload.docContent is not None:
        study.doc_content = payload.docContent
    if payload.dicomSnapshots is not None:
        try:
            from app import storage as file_storage
        except ImportError:
            from app import storage as file_storage
        meta = dict(study.metadata_ or {})
        meta["dicomSnapshots"] = file_storage.materialize_media_list(
            payload.dicomSnapshots,
            category="cases",
            entity_id=study.case_id,
            subfolder="snapshots",
        )
        study.metadata_ = meta


def sync_case_report_maps(db: Session, case: CaseDB, studies: list) -> None:
    rbp, ibp, tbp = {}, {}, {}
    for s in studies:
        if not s.body_part:
            continue
        if s.findings:
            rbp[s.body_part] = s.findings
        if s.impression:
            ibp[s.body_part] = s.impression
        if getattr(s, "technique", None):
            tbp[s.body_part] = s.technique
    meta = dict(case.metadata_ or {})
    meta["reportsByBodyPart"] = rbp
    meta["impressionsByBodyPart"] = ibp
    meta["techniquesByBodyPart"] = tbp
    meta["findings"] = "\n\n".join(
        f"[ {s.body_part} ]\n{s.findings or ''}" for s in studies if s.body_part
    )
    meta["impression"] = "\n\n".join(
        f"[ {s.body_part} ]\n{s.impression or ''}" for s in studies if s.body_part
    )
    case.metadata_ = meta
    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    if report:
        rmeta = dict(report.metadata_ or {})
        for k in ("reportsByBodyPart", "impressionsByBodyPart", "techniquesByBodyPart", "findings", "impression"):
            rmeta[k] = meta.get(k)
        report.metadata_ = rmeta


def _record_billing_failure(db: Session, case: CaseDB, report, err: Exception) -> None:
    """Log loudly and flag the case so the admin can see it is completed but unbilled."""
    logger.error(
        "BILLING FAILED for case %s: report is saved and completed but NOT billed: %s",
        case.id, err, exc_info=True,
    )
    flag = {"message": str(err)[:300], "at": datetime.utcnow().isoformat() + "Z"}
    for row in (case, report):
        if row is not None:
            meta = dict(row.metadata_ or {})
            meta["billingError"] = flag
            row.metadata_ = meta


async def finalize_case_if_complete(db: Session, case: CaseDB, studies: list) -> dict:
    signed, total = signed_count(studies)
    out = {
        "caseComplete": False,
        "signedStudyCount": signed,
        "studyCount": total,
        "pendingStudyCount": max(0, total - signed),
        "billing": None,
        "notified": False,
    }
    if total == 0 or signed < total:
        if (case.status or "") == "Completed":
            case.status = "In Review"
        elif (case.status or "") not in ("In Review",):
            case.status = "In Review"
        report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
        if report and (report.status or "") == "Completed":
            report.status = "In Review"
        return out

    already_complete = (case.status or "") == "Completed"
    case.status = "Completed"
    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    if report:
        report.status = "Completed"
        rmeta = dict(report.metadata_ or {})
        rmeta["signatureApplied"] = True
        report.metadata_ = rmeta
        last = next(
            (s for s in reversed(studies) if getattr(s, "signed_by", None)),
            studies[-1],
        )
        if not report.assigned_doctor_id:
            report.assigned_doctor_id = getattr(last, "signed_by", None) or last.claimed_by
            report.assigned_doctor_name = getattr(last, "signed_by_name", None) or last.claimed_by_name

    if report:
        payload = enrich_case_item(db_to_schema(report), case, studies)
    else:
        payload = enrich_case_item(study_to_schema(studies[0], case), case, studies)

    out["caseComplete"] = True
    # The center "completed" notification is NOT sent here. It is returned as
    # out["completedEvent"] and the caller broadcasts it only after billing has
    # succeeded and db.commit() has gone through (see notify_case_completed).
    # A billing failure (e.g. 409 locked period) therefore sends nothing.
    completed_event = None
    if not already_complete:
        completed_event = {"type": "REPORT_COMPLETED", "report": payload}

    # Billing runs inside a SAVEPOINT: an unexpected billing error rolls back only the
    # billing rows, so the doctor's signed report is still saved (logged loudly and
    # flagged as billingError). A locked period (ValueError) still aborts with 409.
    if not billing_svc.case_already_billed(db, case.id):
        try:
            with db.begin_nested():
                out["billing"] = billing_svc.create_billing_on_sign_off(db, case_id=case.id)
        except ValueError as be:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(be))
        except Exception as be:
            _record_billing_failure(db, case, report, be)
            out["billing"] = {"failed": True, "caseId": case.id}
    else:
        out["billing"] = {"skipped": True, "reason": "already_billed", "caseId": case.id}
        # Center is never billed twice. Doctor payout (per study): if another
        # doctor still holds a study's ACTIVE payout (case reassigned after
        # sign-off), claw it back now and issue the line to the final signer.
        try:
            with db.begin_nested():
                payout = billing_svc.ensure_doctor_payout_for_signer(db, case_id=case.id)
        except ValueError as be:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(be))
        except Exception as be:
            _record_billing_failure(db, case, report, be)
            payout = None
        if payout:
            out["billing"]["doctorPayout"] = payout
    out["completedEvent"] = completed_event
    return out


async def notify_case_completed(finalize: dict) -> bool:
    """Broadcast the center 'case completed' event. Call ONLY after db.commit()."""
    event = (finalize or {}).pop("completedEvent", None)
    if not event:
        return False
    await manager.broadcast(event)
    finalize["notified"] = True
    return True


@router.post("/{case_id}/studies/{study_id}/public-link/revoke")
def revoke_study_public_link(
    case_id: str,
    study_id: str,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Withdraw the public QR link of a signed study. The next sign-off issues a new one."""
    case, study = resolve_case_and_study(db, case_id, study_id)
    revoked = report_links.revoke_public_link(db, study)
    db.commit()
    return {"ok": True, "revoked": revoked, "studyId": study.id}


@router.get("/my-partial-cases")
def get_my_partial_cases(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    keys = {str(k) for k in _doctor_claim_identity_keys(db, current_user)}
    claimed = db.query(StudyDB).filter(StudyDB.claimed_by.in_(list(keys))).all()
    case_ids = {s.case_id for s in claimed}
    items = []
    for case_id in case_ids:
        studies = case_studies_ordered(db, case_id)
        if not is_partial_case(studies):
            continue
        case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
        if not case:
            continue
        signed, total = signed_count(studies)
        items.append({
            "id": case.id,
            "patientName": case.full_name,
            "patientNumber": case.patient_number,
            "signedStudyCount": signed,
            "studyCount": total,
            "pendingStudyCount": max(0, total - signed),
            "pendingBodyParts": [
                s.body_part for s in studies if study_report_status(s) != "SIGNED"
            ],
        })
    return {"items": items, "total": len(items)}


@router.post("/{case_id}/studies/{study_id}/draft")
async def save_study_draft(
    case_id: str,
    study_id: str,
    payload: StudyReportPayload,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    case, study = resolve_case_and_study(db, case_id, study_id)
    doctor_may_edit_study(db, current_user, study)
    if study_report_status(study) == "SIGNED":
        raise HTTPException(status_code=409, detail="Study already signed")
    apply_study_payload(study, payload)
    study.report_status = "DRAFT"
    studies = case_studies_ordered(db, case.id)
    sync_case_report_maps(db, case, studies)
    if (case.status or "") in ("Pending", "", None):
        case.status = "In Review"
    db.commit()
    db.refresh(study)
    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    item = enrich_case_item(
        db_to_schema(report) if report else study_to_schema(study, case),
        case,
        studies,
    )
    return {
        "ok": True,
        "study": {
            "id": study.id,
            "reportStatus": study_report_status(study),
            "findings": study.findings,
            "impression": study.impression,
            "technique": study.technique,
            "templateId": study.template_id,
        },
        "report": item,
    }


@router.post("/{case_id}/studies/{study_id}/sign")
async def sign_study_report(
    case_id: str,
    study_id: str,
    payload: StudyReportPayload,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR", "SUPER_ADMIN", "MANAGER")),
):
    case, study = resolve_case_and_study(db, case_id, study_id)
    doctor_may_edit_study(db, current_user, study)
    already_signed = study_report_status(study) == "SIGNED"
    apply_study_payload(study, payload)
    if not (study.findings and str(study.findings).strip()) and not (
        study.impression and str(study.impression).strip()
    ):
        raise HTTPException(status_code=400, detail="Findings or impression required to sign")
    if not study.technique:
        study.technique = f"{study.modality or 'X-Ray'} - {study.body_part}"

    doctor_id = (current_user.metadata_ or {}).get("doctorId") or study.claimed_by
    doctor_name = current_user.name
    doc = None
    if doctor_id:
        doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
        if doc:
            doctor_name = doc.full_name

    study.report_status = "SIGNED"
    study.status = "Completed"
    study.signed_at = datetime.utcnow().isoformat() + "Z"
    study.signed_by = str(doctor_id) if doctor_id else study.claimed_by
    study.signed_by_name = doctor_name
    # Freeze name / degree / reg no / signature image onto this report (a re-sign refreshes it)
    smeta = dict(study.metadata_ or {})
    smeta["signer"] = build_signer_snapshot(doc, doctor_id, doctor_name, study.signed_at)
    if payload.title is not None and payload.title.strip():
        smeta["title"] = payload.title.strip()[:200]
    study.metadata_ = smeta
    # Unguessable token for the public QR view (kept on re-sign)
    report_links.ensure_public_link(db, study)

    studies = case_studies_ordered(db, case.id)
    sync_case_report_maps(db, case, studies)
    finalize = await finalize_case_if_complete(db, case, studies)
    db.commit()
    # Billing succeeded and the transaction is committed: now tell the center.
    await notify_case_completed(finalize)

    next_pending = next((s for s in studies if study_report_status(s) != "SIGNED"), None)
    signed, total = signed_count(studies)
    toast = f"{study.body_part} signed. {signed} of {total} done."

    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    out_report = enrich_case_item(
        db_to_schema(report) if report else study_to_schema(study, case),
        case,
        studies,
    )

    if not finalize.get("caseComplete"):
        await manager.broadcast({
            "type": "STUDY_SIGNED",
            "caseId": case.id,
            "studyId": study.id,
            "signedStudyCount": signed,
            "studyCount": total,
            "report": out_report,
        })

    return {
        "ok": True,
        "toast": toast,
        "studyId": study.id,
        "bodyPart": study.body_part,
        "reportStatus": "SIGNED",
        "alreadySigned": already_signed,
        "nextStudyId": next_pending.id if next_pending else None,
        "nextBodyPart": next_pending.body_part if next_pending else None,
        "caseComplete": finalize.get("caseComplete"),
        "signedStudyCount": signed,
        "studyCount": total,
        "pendingStudyCount": max(0, total - signed),
        "billing": finalize.get("billing"),
        "notified": finalize.get("notified"),
        "report": out_report,
    }
