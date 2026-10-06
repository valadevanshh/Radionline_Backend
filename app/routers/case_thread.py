"""Priority 6: case flag/reassign, doctor recheck, and durable comment thread."""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

try:
    from app.database import get_db, engine
    from app.models import (
        Base,
        CaseDB,
        StudyDB,
        StudyNotificationDB,
        DoctorDB,
        UserDB,
        ReportCommentDB,
    )
    from app.schemas import (
        ReportCommentCreate,
        FlagReassignRequest,
        RecheckRequest,
        ReportCommentResponse,
    )
    from app.security import get_current_user, require_roles
    from app.websocket import manager
    from app.models import ReportDB
    from app.routers.study_reports import study_report_status
    from app import billing as billing_svc
    from app import notifications as notif_svc
    from app import access as access_svc
except ImportError:
    from app.database import get_db, engine
    from app.models import (
        Base,
        CaseDB,
        StudyDB,
        StudyNotificationDB,
        DoctorDB,
        UserDB,
        ReportCommentDB,
    )
    from app.schemas import (
        ReportCommentCreate,
        FlagReassignRequest,
        RecheckRequest,
        ReportCommentResponse,
    )
    from app.security import get_current_user, require_roles
    from app.websocket import manager
    from app.models import ReportDB
    from app.routers.study_reports import study_report_status
    from app import billing as billing_svc
    from app import notifications as notif_svc
    from app import access as access_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/reports",
    tags=["Case Thread"],
    dependencies=[Depends(get_current_user)],
)

COMMENT_KINDS = {"FLAG", "REASSIGN", "RECHECK", "COMMENT"}


def ensure_report_comment_tables(bind=None) -> None:
    """Create report_comments if missing (Postgres + SQLite)."""
    target = bind or engine
    Base.metadata.create_all(bind=target, tables=[ReportCommentDB.__table__])
    logger.info("Ensured report_comments table exists (dialect=%s)", target.dialect.name)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _comment_to_schema(row: ReportCommentDB) -> dict:
    return {
        "id": row.id,
        "caseId": row.case_id,
        "studyId": row.study_id,
        "authorUserId": row.author_user_id,
        "authorRole": row.author_role,
        "authorName": row.author_name,
        "kind": row.kind,
        "body": row.body,
        "fromDoctorId": row.from_doctor_id,
        "toDoctorId": row.to_doctor_id,
        "createdAt": row.created_at,
    }


def _resolve_case(db: Session, report_id: str) -> CaseDB:
    case = db.query(CaseDB).filter(CaseDB.id == report_id).first()
    if case:
        return case
    study = (
        db.query(StudyDB)
        .filter((StudyDB.id == report_id) | (StudyDB.case_id == report_id))
        .first()
    )
    if study:
        case = db.query(CaseDB).filter(CaseDB.id == study.case_id).first()
        if case:
            return case
    raise HTTPException(status_code=404, detail="Case/report not found")


def _case_studies(db: Session, case_id: str) -> List[StudyDB]:
    return db.query(StudyDB).filter(StudyDB.case_id == case_id).all()


def _doctor_identity_keys(db: Session, user: UserDB) -> set:
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


def _can_access_case(db: Session, user: UserDB, case: CaseDB) -> bool:
    if user.role in ("SUPER_ADMIN", "MANAGER"):
        return True
    if user.role == "CENTER":
        # Multi-centre logins: any linked centre with Reports >= read
        return access_svc.has_center_perm(db, user, case.radiology_center_id, "reports", "read")
    if user.role == "DOCTOR":
        try:
            keys = _doctor_identity_keys(db, user)
        except HTTPException:
            return False
        studies = _case_studies(db, case.id)
        if any(s.claimed_by and s.claimed_by in keys for s in studies):
            return True
        study_ids = [s.id for s in studies]
        if study_ids:
            notif = (
                db.query(StudyNotificationDB)
                .filter(
                    StudyNotificationDB.study_id.in_(study_ids),
                    StudyNotificationDB.doctor_id.in_(list(keys)),
                )
                .first()
            )
            if notif:
                return True
        return False
    return False


def _require_case_access(db: Session, user: UserDB, case: CaseDB) -> None:
    if not _can_access_case(db, user, case):
        raise HTTPException(status_code=403, detail="Not allowed to access this case")


def _center_owns_case(user: UserDB, case: CaseDB, db: Optional[Session] = None, level: str = "read") -> bool:
    """Staff, or a centre login linked to the case's centre with Reports >= level."""
    if user.role in ("SUPER_ADMIN", "MANAGER"):
        return True
    if user.role == "CENTER":
        if db is None:
            center_id = (user.metadata_ or {}).get("centerId")
            return bool(center_id) and case.radiology_center_id == center_id
        return access_svc.has_center_perm(db, user, case.radiology_center_id, "reports", level)
    return False


def _add_comment_row(
    db: Session,
    *,
    case_id: str,
    study_id: Optional[str],
    user: UserDB,
    kind: str,
    body: str,
    from_doctor_id: Optional[str] = None,
    to_doctor_id: Optional[str] = None,
) -> ReportCommentDB:
    row = ReportCommentDB(
        case_id=case_id,
        study_id=study_id,
        author_user_id=user.id,
        author_role=user.role,
        author_name=user.name,
        kind=kind,
        body=body.strip(),
        from_doctor_id=from_doctor_id,
        to_doctor_id=to_doctor_id,
        created_at=_now_iso(),
    )
    db.add(row)
    db.flush()
    return row


def _notify_doctor_for_studies(
    db: Session, studies: List[StudyDB], doctor_id: str, when: str
) -> None:
    for study in studies:
        existing = (
            db.query(StudyNotificationDB)
            .filter(
                StudyNotificationDB.study_id == study.id,
                StudyNotificationDB.doctor_id == doctor_id,
            )
            .first()
        )
        if existing:
            existing.dismissed_reason = None
            existing.notified_at = when
        else:
            db.add(
                StudyNotificationDB(
                    study_id=study.id,
                    doctor_id=doctor_id,
                    notified_at=when,
                    dismissed_reason=None,
                )
            )
        # Mark other doctors' open notifications as accepted_other
        db.query(StudyNotificationDB).filter(
            StudyNotificationDB.study_id == study.id,
            StudyNotificationDB.doctor_id != doctor_id,
            StudyNotificationDB.dismissed_reason.is_(None),
        ).update({"dismissed_reason": "accepted_other"}, synchronize_session=False)


REEDITABLE_REPORT_STATUS = "DRAFT"  # existing per-study re-editable status (PENDING | DRAFT | SIGNED)
ACTIVE_CASE_STATUS = "In Review"     # existing active case status


def _snapshot_and_unsign_study(
    study: StudyDB, *, event: str, reason: str, actor: UserDB, when: str
) -> dict:
    """
    Append an immutable snapshot of the signed report to
    studies.metadata.reportHistory (append-only; never trimmed), then revert the
    study to the re-editable DRAFT state. Findings/impression stay in place so
    the next doctor edits from the signed text; the original signer's content
    and signature live on in the snapshot.
    """
    meta = dict(study.metadata_ or {})
    history = list(meta.get("reportHistory") or [])
    snap = {
        "version": len(history) + 1,
        "event": event,
        "reason": reason,
        "snapshotAt": when,
        "snapshotByUserId": actor.id,
        "snapshotByName": actor.name,
        "snapshotByRole": actor.role,
        "reportStatus": study_report_status(study),
        "studyStatus": study.status,
        "findings": study.findings,
        "impression": study.impression,
        "technique": getattr(study, "technique", None),
        "templateId": getattr(study, "template_id", None),
        "docContent": study.doc_content,
        "clinicalNotes": study.clinical_notes,
        "signedAt": getattr(study, "signed_at", None),
        "signedBy": getattr(study, "signed_by", None),
        "signedByName": getattr(study, "signed_by_name", None),
        "claimedBy": study.claimed_by,
        "claimedByName": study.claimed_by_name,
    }
    history.append(snap)
    meta["reportHistory"] = history
    study.metadata_ = meta
    study.report_status = REEDITABLE_REPORT_STATUS
    study.status = "CLAIMED"
    study.signed_at = None
    study.signed_by = None
    study.signed_by_name = None
    return snap


def _revert_case_to_active(db: Session, case: CaseDB) -> None:
    if (case.status or "") in ("Completed", "Pending", ""):
        case.status = ACTIVE_CASE_STATUS
    report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
    if report:
        if (report.status or "") in ("Completed", "Pending", ""):
            report.status = ACTIVE_CASE_STATUS
        rmeta = dict(report.metadata_ or {})
        if rmeta.get("signatureApplied"):
            rmeta["signatureApplied"] = False
            report.metadata_ = rmeta


@router.get("/{report_id}/comments", response_model=List[ReportCommentResponse])
def list_comments(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    case = _resolve_case(db, report_id)
    _require_case_access(db, current_user, case)
    rows = (
        db.query(ReportCommentDB)
        .filter(ReportCommentDB.case_id == case.id)
        .order_by(ReportCommentDB.created_at.asc(), ReportCommentDB.id.asc())
        .all()
    )
    return [_comment_to_schema(r) for r in rows]


@router.post("/{report_id}/comments", response_model=ReportCommentResponse)
async def add_comment(
    report_id: str,
    payload: ReportCommentCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment body is required")
    case = _resolve_case(db, report_id)
    _require_case_access(db, current_user, case)
    if current_user.role not in ("SUPER_ADMIN", "MANAGER", "CENTER", "DOCTOR"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if current_user.role == "CENTER" and not _center_owns_case(current_user, case, db, "read"):
        raise HTTPException(status_code=403, detail="Cross-center access denied")

    studies = _case_studies(db, case.id)
    study_id = studies[0].id if studies else None
    row = _add_comment_row(
        db,
        case_id=case.id,
        study_id=study_id,
        user=current_user,
        kind="COMMENT",
        body=body,
    )
    db.commit()
    db.refresh(row)
    result = _comment_to_schema(row)
    await manager.broadcast(
        {
            "type": "CASE_THREAD_UPDATED",
            "caseId": case.id,
            "reportId": case.id,
            "comment": result,
        }
    )
    # Bell + live notice: center user(s), Super Admins, Managers and the
    # assigned doctor(s); never the sender.
    await notif_svc.notify_case_message(
        db, case, [s.claimed_by for s in studies], current_user, body
    )
    return result


@router.post("/{report_id}/flag-reassign")
async def flag_and_reassign(
    report_id: str,
    payload: FlagReassignRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(
        require_roles("CENTER", "MANAGER", "SUPER_ADMIN")
    ),
):
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason is required")
    to_doctor_id = (payload.toDoctorId or "").strip()
    if not to_doctor_id:
        raise HTTPException(status_code=400, detail="toDoctorId is required")

    case = _resolve_case(db, report_id)
    if not _center_owns_case(current_user, case, db, "write"):
        raise HTTPException(status_code=403, detail="Cross-center access denied")

    target = db.query(DoctorDB).filter(DoctorDB.id == to_doctor_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target doctor not found")

    studies = _case_studies(db, case.id)
    if not studies:
        raise HTTPException(status_code=404, detail="No studies found for case")

    # Locked month: if the case is already billed and the new doctor's sign-off
    # would have to claw back another doctor's payout lines that sit on a locked
    # invoice period, refuse up front (read-only check, nothing written yet).
    # Not-yet-billed cases and A->B->A (target already holds the lines) pass.
    locked_periods = billing_svc.locked_periods_blocking_reassign(
        db, case_id=case.id, target_doctor_ref=target.id
    )
    if locked_periods:
        label = " and ".join(billing_svc.period_label(p) for p in locked_periods)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This case's billing for {label} is locked, so it can't be reassigned. "
                f"Ask the Super Admin to unlock the period first."
            ),
        )

    from_doctor_id = studies[0].claimed_by
    from_doctor_name = studies[0].claimed_by_name
    when = _now_iso()

    # Fully signed (completed) case: every study moves to the new doctor and is
    # snapshotted + reverted to DRAFT so they can edit and re-sign.
    # Partially signed case (paused per-study flow): unchanged behaviour —
    # already-signed studies stay with the original doctor, pending ones move.
    fully_signed = all(study_report_status(s) == "SIGNED" for s in studies)
    moved = []
    reverted = []
    for study in studies:
        if study_report_status(study) == "SIGNED":
            if not fully_signed:
                continue
            _snapshot_and_unsign_study(
                study, event="REASSIGN", reason=reason, actor=current_user, when=when
            )
            reverted.append(study.id)
        study.claimed_by = target.id
        study.claimed_by_name = target.full_name
        study.status = "CLAIMED"
        moved.append(study)

    unsigned = [s for s in studies if study_report_status(s) != "SIGNED"]
    if unsigned:
        _revert_case_to_active(db, case)

    if moved:
        report = db.query(ReportDB).filter(ReportDB.id == case.id).first()
        if report:
            report.assigned_doctor_id = target.id
            report.assigned_doctor_name = target.full_name
            rmeta = dict(report.metadata_ or {})
            rmeta["claimedByDoctorId"] = target.id
            rmeta["claimedByDoctorName"] = target.full_name
            rmeta["claimStatus"] = "CLAIMED"
            report.metadata_ = rmeta

    # Doctor payout: nothing changes at reassignment. The original doctor's
    # payout lines stay ACTIVE; any clawback happens only when the new doctor
    # re-signs and the case completes (billing.ensure_doctor_payout_for_signer).

    flag_row = _add_comment_row(
        db,
        case_id=case.id,
        study_id=studies[0].id,
        user=current_user,
        kind="FLAG",
        body=reason,
        from_doctor_id=from_doctor_id,
        to_doctor_id=target.id,
    )
    reassign_row = _add_comment_row(
        db,
        case_id=case.id,
        study_id=studies[0].id,
        user=current_user,
        kind="REASSIGN",
        body=reason,
        from_doctor_id=from_doctor_id,
        to_doctor_id=target.id,
    )

    _notify_doctor_for_studies(db, moved if moved else studies, target.id, when)

    db.commit()
    db.refresh(flag_row)
    db.refresh(reassign_row)

    event = {
        "type": "REPORT_REASSIGNED",
        "caseId": case.id,
        "reportId": case.id,
        "fromDoctorId": from_doctor_id,
        "fromDoctorName": from_doctor_name,
        "toDoctorId": target.id,
        "toDoctorName": target.full_name,
        "reason": reason,
        "centerId": case.radiology_center_id,
        "patientName": case.full_name,
        "caseStatus": case.status,
        "flag": _comment_to_schema(flag_row),
        "reassign": _comment_to_schema(reassign_row),
    }
    await manager.broadcast(event)
    # Bell + live notice for the doctor the case now belongs to (not the sender)
    await notif_svc.notify_case_reassigned(db, case, target.id, current_user, reason)

    return {
        "status": "success",
        "caseId": case.id,
        "claimedBy": target.id,
        "claimedByName": target.full_name,
        "fromDoctorId": from_doctor_id,
        "caseStatus": case.status,
        "revertedStudyIds": reverted,
        "flag": _comment_to_schema(flag_row),
        "reassign": _comment_to_schema(reassign_row),
    }


@router.post("/{report_id}/recheck")
async def request_recheck(
    report_id: str,
    payload: RecheckRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(require_roles("DOCTOR")),
):
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason is required")

    case = _resolve_case(db, report_id)
    keys = _doctor_identity_keys(db, current_user)
    studies = _case_studies(db, case.id)
    if not studies:
        raise HTTPException(status_code=404, detail="No studies found for case")

    owns = any(s.claimed_by and s.claimed_by in keys for s in studies)
    if not owns:
        raise HTTPException(
            status_code=403,
            detail="Only the claiming doctor can request recheck",
        )

    if case.status != "Completed":
        # Also accept if all studies completed
        all_completed = all(s.status == "Completed" for s in studies)
        if not all_completed:
            raise HTTPException(
                status_code=400,
                detail="Recheck is only allowed on completed reports",
            )

    # Only the requesting doctor's OWN studies are reopened (signed_by matches
    # the doctor; claimed_by as fallback for rows without signed_by). Other
    # doctors' signed studies stay SIGNED and untouched.
    def _is_own(s) -> bool:
        signer = getattr(s, "signed_by", None)
        if signer:
            return str(signer) in keys
        return bool(s.claimed_by) and str(s.claimed_by) in keys

    own = [s for s in studies if _is_own(s)]
    # Reopenable = own SIGNED studies, plus own legacy rows marked Completed
    # without a per-study report_status (handled by the elif below, as before).
    own_reopenable = [
        s for s in own
        if study_report_status(s) == "SIGNED" or (s.status or "") == "Completed"
    ]
    if not own_reopenable:
        raise HTTPException(
            status_code=400,
            detail="You have no signed studies in this case to recheck",
        )

    # Revert immediately: own signed studies -> snapshot + DRAFT, case -> In Review.
    # Same doctor keeps the claim, so the existing doctor payout stays ACTIVE and
    # re-signing does not create a second payout (see billing.ensure_doctor_payout_for_signer).
    when = _now_iso()
    reverted = []
    for s in own:
        if study_report_status(s) == "SIGNED":
            _snapshot_and_unsign_study(
                s, event="RECHECK", reason=reason, actor=current_user, when=when
            )
            reverted.append(s.id)
        elif (s.status or "") == "Completed":
            s.status = "CLAIMED"
    _revert_case_to_active(db, case)

    doctor_id = (current_user.metadata_ or {}).get("doctorId")
    row = _add_comment_row(
        db,
        case_id=case.id,
        study_id=studies[0].id,
        user=current_user,
        kind="RECHECK",
        body=reason,
        from_doctor_id=str(doctor_id) if doctor_id else studies[0].claimed_by,
    )
    db.commit()
    db.refresh(row)
    result = _comment_to_schema(row)

    await manager.broadcast(
        {
            "type": "REPORT_RECHECK",
            "caseId": case.id,
            "reportId": case.id,
            "centerId": case.radiology_center_id,
            "doctorId": doctor_id,
            "doctorName": current_user.name,
            "patientName": case.full_name,
            "reason": reason,
            "caseStatus": case.status,
            "comment": result,
        }
    )
    return {
        "status": "success",
        "caseId": case.id,
        "caseStatus": case.status,
        "revertedStudyIds": reverted,
        "recheck": result,
    }
