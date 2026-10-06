"""Billing & invoicing service — sign-off accrual, period lock, status (IST)."""
from __future__ import annotations

import calendar as _calendar
import hashlib
import logging
import time
import uuid
from datetime import datetime
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

try:
    from app.models import (
        CaseDB,
        StudyDB,
        InvoiceDB,
        InvoiceLineItemDB,
        BillingPeriodLockDB,
        DoctorDB,
        CenterDB,
        CenterPricingDB,
    )
    from app import pricing
except ImportError:
    from app.models import (
        CaseDB,
        StudyDB,
        InvoiceDB,
        InvoiceLineItemDB,
        BillingPeriodLockDB,
        DoctorDB,
        CenterDB,
        CenterPricingDB,
    )
    from app import pricing

logger = logging.getLogger(__name__)
try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    try:
        IST = ZoneInfo("Asia/Calcutta")
    except Exception:
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def now_ist_iso() -> str:
    return now_ist().isoformat()


def current_billing_period() -> str:
    """YYYY-MM in Asia/Calcutta."""
    return now_ist().strftime("%Y-%m")


def period_from_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt.strftime("%Y-%m")


def is_period_locked(db: Session, period: str) -> bool:
    row = db.query(BillingPeriodLockDB).filter(BillingPeriodLockDB.period == period).first()
    return bool(row and row.locked)


def compute_effective_status(stored_status: str, billing_period: str, as_of: Optional[datetime] = None) -> str:
    """
    paid: when Super Admin marks paid (stored).
    pending: unpaid and current calendar month (IST) <= billing month.
    overdue: unpaid and current calendar month (IST) > billing month.
    """
    if (stored_status or "").lower() == "paid":
        return "paid"
    as_of = as_of or now_ist()
    current = as_of.strftime("%Y-%m")
    if current > billing_period:
        return "overdue"
    return "pending"


def _invoice_id(party_type: str, party_id: str, period: str) -> str:
    safe_party = (party_id or "unknown").replace(" ", "-")
    # invoices.id is VARCHAR(100): ids that fit are unchanged; longer ones get a stable hashed id.
    return _bounded_id(f"inv-{party_type}-{safe_party}-{period}", f"inv-{party_type}")


def _get_or_create_open_invoice(
    db: Session,
    *,
    party_type: str,
    party_id: str,
    party_name: str,
    period: str,
) -> InvoiceDB:
    inv_id = _invoice_id(party_type, party_id, period)
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == inv_id).first()
    ts = now_ist_iso()
    if inv:
        if inv.locked or is_period_locked(db, period):
            raise ValueError(f"Billing period {period} is locked; cannot modify invoice {inv_id}")
        return inv
    if is_period_locked(db, period):
        raise ValueError(f"Billing period {period} is locked; cannot create invoice for {party_type}/{party_id}")
    inv = InvoiceDB(
        id=inv_id,
        party_type=party_type,
        party_id=party_id,
        party_name=party_name or party_id,
        billing_period=period,
        status="pending",
        locked=False,
        total_amount=0,
        currency=pricing.CURRENCY,
        linked_invoice_id=None,
        created_at=ts,
        updated_at=ts,
        metadata_={},
    )
    db.add(inv)
    db.flush()
    return inv


def _recompute_invoice_total(db: Session, invoice_id: str) -> None:
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == invoice_id).first()
    if not inv or inv.locked:
        return
    lines = db.query(InvoiceLineItemDB).filter(InvoiceLineItemDB.invoice_id == invoice_id).all()
    inv.total_amount = sum(int(li.amount or 0) for li in lines)
    inv.updated_at = now_ist_iso()


def case_already_billed(db: Session, case_id: str) -> bool:
    existing = (
        db.query(InvoiceLineItemDB)
        .filter(InvoiceLineItemDB.case_id == case_id)
        .first()
    )
    return existing is not None


def billing_ordered_studies(db: Session, case_id: str) -> List[StudyDB]:
    """
    Study order used ONLY for billing (study_index -> index price, center and
    doctor lines, re-issued payouts). If every study of the case has a
    sequence_no (upload order of bodyParts), order by sequence_no, then id.
    Otherwise (legacy cases, never backfilled) keep the original
    ORDER BY created_at, id so existing cases price exactly as before.
    Display ordering (study_reports.case_studies_ordered) is not affected.
    """
    studies = (
        db.query(StudyDB)
        .filter(StudyDB.case_id == case_id)
        .order_by(StudyDB.created_at.asc(), StudyDB.id.asc())
        .all()
    )
    if studies and all(getattr(s, "sequence_no", None) is not None for s in studies):
        studies = sorted(studies, key=lambda s: (int(s.sequence_no), s.id))
    return studies


def create_billing_on_sign_off(
    db: Session,
    *,
    case_id: str,
    period: Optional[str] = None,
) -> Optional[dict]:
    """
    On report sign-off: accrue line items onto open monthly center + doctor invoices.
    Idempotent per case_id. Totals on locked invoices never change.
    Returns summary dict or None if skipped.
    """
    if case_already_billed(db, case_id):
        logger.info("Billing skipped — case %s already has invoice line items", case_id)
        return {"skipped": True, "reason": "already_billed", "caseId": case_id}

    case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    if not case:
        logger.warning("Billing skipped — case %s not found", case_id)
        return None

    studies = billing_ordered_studies(db, case_id)
    if not studies:
        logger.warning("Billing skipped — case %s has no studies", case_id)
        return None

    period = period or current_billing_period()
    if is_period_locked(db, period):
        raise ValueError(
            f"Billing for {period_label(period)} is locked, so this case can't be completed yet. "
            f"Ask the Super Admin to unlock {period_label(period)}, then sign again."
        )

    center_id = case.radiology_center_id
    center_name = case.radiology_center_name or center_id
    # This centre's rate card at sign-off time (defaults when it has none).
    rates = rates_for_center(db, center_id)

    # Doctor payout attribution is PER STUDY: each study's line goes to the doctor
    # who signed that study (signed_by, else claimed_by), priced by the study's
    # index in the case (same study_index as the center line). Case-level
    # metadata is only a fallback for legacy studies with no claim/sign info.
    meta = case.metadata_ or {}
    fallback_id = meta.get("claimedByDoctorId") or meta.get("assignedDoctorId")
    fallback_name = meta.get("claimedByDoctorName") or meta.get("assignedDoctorName")

    def _study_doctor(study):
        ref = getattr(study, "signed_by", None) or study.claimed_by
        name = getattr(study, "signed_by_name", None) or study.claimed_by_name or ref
        if not ref:
            ref, name = fallback_id, fallback_name
        if ref in (None, "", "ALL"):
            # Still create center charge; doctor payout needs a real doctor
            return None, None
        doc = db.query(DoctorDB).filter(DoctorDB.id == ref).first()
        return ref, (doc.full_name if doc else (name or ref))

    center_inv = _get_or_create_open_invoice(
        db,
        party_type="center",
        party_id=center_id,
        party_name=center_name,
        period=period,
    )

    doctor_invs = {}  # doctor_id -> InvoiceDB

    def _doctor_inv(doctor_id, doctor_name):
        inv = doctor_invs.get(doctor_id)
        if inv is None:
            inv = _get_or_create_open_invoice(
                db,
                party_type="doctor",
                party_id=doctor_id,
                party_name=doctor_name or doctor_id,
                period=period,
            )
            # Soft link monthly invoices for this center<->doctor pair within the period
            if not center_inv.linked_invoice_id:
                center_inv.linked_invoice_id = inv.id
            if not inv.linked_invoice_id:
                inv.linked_invoice_id = center_inv.id
            doctor_invs[doctor_id] = inv
        return inv

    billing_event_id = f"be-{case_id}-{int(time.time() * 1000)}"
    ts = now_ist_iso()
    # Prefer study_date on case for per-day breakdown; normalize to DD-MM-YYYY if possible
    service_date = case.study_date or now_ist().strftime("%d-%m-%Y")

    center_lines_created = []
    doctor_lines_created = []
    doctor_attribution = []
    doctor_total = 0
    n = len(studies)

    for idx, study in enumerate(studies, start=1):
        c_amt = rates.center_amount(idx)
        d_amt = rates.doctor_amount(idx)
        doctor_id, doctor_name = _study_doctor(study)
        doctor_inv = _doctor_inv(doctor_id, doctor_name) if doctor_id else None
        # Line ids are VARCHAR(100). study.id already contains case_id, so long body-part
        # names overflowed (500 on sign, report not saved). Ids that fit stay byte-identical.
        c_line_id = _bounded_id(f"ili-c-{case_id}-{study.id}-{idx}", "ili-c")
        d_line_id = _bounded_id(f"ili-d-{case_id}-{study.id}-{idx}", "ili-d") if doctor_inv else None

        c_line = InvoiceLineItemDB(
            id=c_line_id,
            invoice_id=center_inv.id,
            paired_line_item_id=d_line_id,
            billing_event_id=billing_event_id,
            case_id=case_id,
            study_id=study.id,
            study_index=idx,
            service_date=service_date,
            description=f"Study {idx}/{n}: {study.body_part} ({study.modality or 'X-Ray'})",
            modality=study.modality,
            body_part=study.body_part,
            patient_number=case.patient_number,
            patient_name=case.full_name,
            center_id=center_id,
            center_name=center_name,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            unit_amount=c_amt,
            quantity=1,
            amount=c_amt,
            currency=pricing.CURRENCY,
            created_at=ts,
            status=ACTIVE,
        )
        db.add(c_line)
        center_lines_created.append(c_line_id)

        if doctor_inv and d_line_id:
            d_line = InvoiceLineItemDB(
                id=d_line_id,
                invoice_id=doctor_inv.id,
                paired_line_item_id=c_line_id,
                billing_event_id=billing_event_id,
                case_id=case_id,
                study_id=study.id,
                study_index=idx,
                service_date=service_date,
                description=f"Payout study {idx}/{n}: {study.body_part} ({study.modality or 'X-Ray'})",
                modality=study.modality,
                body_part=study.body_part,
                patient_number=case.patient_number,
                patient_name=case.full_name,
                center_id=center_id,
                center_name=center_name,
                doctor_id=doctor_id,
                doctor_name=doctor_name,
                unit_amount=d_amt,
                quantity=1,
                amount=d_amt,
                currency=pricing.CURRENCY,
                created_at=ts,
                status=ACTIVE,
            )
            db.add(d_line)
            doctor_lines_created.append(d_line_id)
            doctor_total += d_amt
            doctor_attribution.append(
                {"studyId": study.id, "studyIndex": idx, "doctorId": doctor_id,
                 "doctorName": doctor_name, "amount": d_amt, "lineId": d_line_id}
            )

    db.flush()
    _recompute_invoice_total(db, center_inv.id)
    for inv in doctor_invs.values():
        _recompute_invoice_total(db, inv.id)

    first_doctor_inv = next(iter(doctor_invs.values()), None)
    summary = {
        "billingEventId": billing_event_id,
        "caseId": case_id,
        "period": period,
        "studyCount": n,
        "centerInvoiceId": center_inv.id,
        "doctorInvoiceId": first_doctor_inv.id if first_doctor_inv else None,
        "doctorInvoiceIds": [inv.id for inv in doctor_invs.values()],
        "centerTotalAdded": rates.center_total(n),
        "rates": rates.as_dict(),
        "doctorTotalAdded": doctor_total,
        "centerLineIds": center_lines_created,
        "doctorLineIds": doctor_lines_created,
        "doctorAttribution": doctor_attribution,
    }
    logger.info("Billing created on sign-off: %s", summary)
    return summary


def lock_billing_period(db: Session, period: str, locked_by: str) -> dict:
    """Finalize all invoices for YYYY-MM so totals cannot change."""
    if not period or len(period) != 7 or period[4] != "-":
        raise ValueError("period must be YYYY-MM")

    ts = now_ist_iso()
    lock = db.query(BillingPeriodLockDB).filter(BillingPeriodLockDB.period == period).first()
    if not lock:
        lock = BillingPeriodLockDB(period=period, locked=True, locked_at=ts, locked_by=locked_by)
        db.add(lock)
    else:
        lock.locked = True
        lock.locked_at = ts
        lock.locked_by = locked_by

    invoices = db.query(InvoiceDB).filter(InvoiceDB.billing_period == period).all()
    for inv in invoices:
        # Finalize snapshot total from line items once, then lock
        lines = db.query(InvoiceLineItemDB).filter(InvoiceLineItemDB.invoice_id == inv.id).all()
        inv.total_amount = sum(int(li.amount or 0) for li in lines)
        inv.locked = True
        inv.updated_at = ts
        # Refresh overdue/pending at lock time (do not change paid)
        if (inv.status or "").lower() != "paid":
            inv.status = "pending"  # effective overdue computed on read

    db.flush()
    return {
        "period": period,
        "locked": True,
        "lockedAt": ts,
        "lockedBy": locked_by,
        "invoicesFinalized": len(invoices),
    }


def unlock_billing_period(db: Session, period: str, unlocked_by: str) -> dict:
    """Optional unlock for Super Admin corrections — totals remain as stored until new lines added."""
    lock = db.query(BillingPeriodLockDB).filter(BillingPeriodLockDB.period == period).first()
    ts = now_ist_iso()
    if lock:
        lock.locked = False
        lock.locked_at = ts
        lock.locked_by = unlocked_by
    invoices = db.query(InvoiceDB).filter(InvoiceDB.billing_period == period).all()
    for inv in invoices:
        inv.locked = False
        inv.updated_at = ts
    db.flush()
    return {"period": period, "locked": False, "unlockedBy": unlocked_by, "invoicesUnlocked": len(invoices)}


def set_invoice_status(db: Session, invoice_id: str, status: str) -> InvoiceDB:
    status = (status or "").lower().strip()
    if status not in ("paid", "pending"):
        raise ValueError("status must be paid or pending")
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == invoice_id).first()
    if not inv:
        raise KeyError("invoice not found")
    inv.status = status
    inv.updated_at = now_ist_iso()
    db.flush()
    return inv


def invoice_to_dict(inv: InvoiceDB, include_effective_status: bool = True) -> dict:
    eff = compute_effective_status(inv.status, inv.billing_period) if include_effective_status else inv.status
    return {
        "id": inv.id,
        "partyType": inv.party_type,
        "partyId": inv.party_id,
        "partyName": inv.party_name,
        "billingPeriod": inv.billing_period,
        "status": eff,
        "storedStatus": inv.status,
        "locked": bool(inv.locked),
        "totalAmount": int(inv.total_amount or 0),
        "currency": inv.currency or "INR",
        "linkedInvoiceId": inv.linked_invoice_id,
        "createdAt": inv.created_at,
        "updatedAt": inv.updated_at,
    }


def line_item_to_dict(li: InvoiceLineItemDB) -> dict:
    return {
        "id": li.id,
        "invoiceId": li.invoice_id,
        "pairedLineItemId": li.paired_line_item_id,
        "billingEventId": li.billing_event_id,
        "caseId": li.case_id,
        "studyId": li.study_id,
        "studyIndex": li.study_index,
        "serviceDate": li.service_date,
        "description": li.description,
        "modality": li.modality,
        "bodyPart": li.body_part,
        "patientNumber": li.patient_number,
        "patientName": li.patient_name,
        "centerId": li.center_id,
        "centerName": li.center_name,
        "doctorId": li.doctor_id,
        "doctorName": li.doctor_name,
        "unitAmount": int(li.unit_amount or 0),
        "quantity": int(li.quantity or 1),
        "amount": int(li.amount or 0),
        "currency": li.currency or "INR",
        "createdAt": li.created_at,
        "status": line_item_status(li),
        "statusReason": getattr(li, "status_reason", None),
        "offsetsLineItemId": getattr(li, "offsets_line_item_id", None),
    }


def group_lines_by_day(lines: List[InvoiceLineItemDB]) -> List[dict]:
    by_day: dict = {}
    for li in lines:
        day = li.service_date or "unknown"
        if day not in by_day:
            by_day[day] = {"serviceDate": day, "studies": [], "dayTotal": 0}
        by_day[day]["studies"].append(line_item_to_dict(li))
        by_day[day]["dayTotal"] += int(li.amount or 0)
    # stable sort by date string
    return [by_day[k] for k in sorted(by_day.keys())]


def group_lines_by_center(lines: List[InvoiceLineItemDB]) -> List[dict]:
    by_center: dict = {}
    for li in lines:
        cid = li.center_id or "unknown"
        if cid not in by_center:
            by_center[cid] = {
                "centerId": cid,
                "centerName": li.center_name or cid,
                "studies": [],
                "centerTotal": 0,
            }
        by_center[cid]["studies"].append(line_item_to_dict(li))
        by_center[cid]["centerTotal"] += int(li.amount or 0)
    return list(by_center.values())


def ensure_billing_tables(engine) -> None:
    """Create billing tables if missing (Postgres + SQLite)."""
    try:
        from app.models import Base
    except ImportError:
        from app.models import Base
    tables = [
        BillingPeriodLockDB.__table__,
        InvoiceDB.__table__,
        InvoiceLineItemDB.__table__,
        CenterPricingDB.__table__,
    ]
    Base.metadata.create_all(bind=engine, tables=tables)
    logger.info("Ensured billing tables exist: %s", [t.name for t in tables])
    ensure_line_item_clawback_columns(engine)


# P6 reassign fix: additive, nullable columns for doctor payout clawback tracking.
LINE_ITEM_CLAWBACK_COLUMNS = {
    "status": "VARCHAR(20) DEFAULT 'ACTIVE'",
    "status_reason": "TEXT",
    "offsets_line_item_id": "VARCHAR(100)",
}


def ensure_line_item_clawback_columns(engine) -> None:
    """ALTER TABLE invoice_line_items ADD COLUMN ... (Postgres + SQLite), additive only."""
    from sqlalchemy import text as sa_text, inspect as sa_inspect

    dialect = engine.dialect.name
    try:
        have = {c["name"] for c in sa_inspect(engine).get_columns("invoice_line_items")}
    except Exception:
        have = set()
    with engine.begin() as conn:
        for col, ddl in LINE_ITEM_CLAWBACK_COLUMNS.items():
            if col in have:
                continue
            if dialect == "postgresql":
                conn.execute(sa_text(f"ALTER TABLE invoice_line_items ADD COLUMN IF NOT EXISTS {col} {ddl}"))
            else:
                conn.execute(sa_text(f"ALTER TABLE invoice_line_items ADD COLUMN {col} {ddl}"))
            logger.info("Added invoice_line_items.%s", col)


# ---------------------------------------------------------------------------
# Doctor payout clawback (case reassigned after sign-off) + re-issue to signer
# ---------------------------------------------------------------------------
ACTIVE = "ACTIVE"
CLAWED_BACK = "CLAWED_BACK"
CLAWBACK = "CLAWBACK"


def line_item_status(li: InvoiceLineItemDB) -> str:
    raw = (getattr(li, "status", None) or "").strip().upper()
    return raw or ACTIVE


def _doctor_lines_for_case(db: Session, case_id: str) -> List[InvoiceLineItemDB]:
    return (
        db.query(InvoiceLineItemDB)
        .join(InvoiceDB, InvoiceDB.id == InvoiceLineItemDB.invoice_id)
        .filter(InvoiceLineItemDB.case_id == case_id, InvoiceDB.party_type == "doctor")
        .order_by(InvoiceLineItemDB.created_at.asc(), InvoiceLineItemDB.study_index.asc())
        .all()
    )


def active_doctor_payout_lines(db: Session, case_id: str) -> List[InvoiceLineItemDB]:
    """Positive, non-offset, non-clawed-back doctor payout lines for a case."""
    return [
        li
        for li in _doctor_lines_for_case(db, case_id)
        if line_item_status(li) == ACTIVE
        and int(li.amount or 0) > 0
        and not getattr(li, "offsets_line_item_id", None)
    ]


def _doctor_keys(db: Session, doctor_ref: Optional[str]) -> Tuple[Optional[DoctorDB], set]:
    """Resolve a doctor id/email/username to the DoctorDB row + all identity keys."""
    if not doctor_ref:
        return None, set()
    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_ref).first()
    if not doc:
        doc = (
            db.query(DoctorDB)
            .filter((DoctorDB.email == doctor_ref) | (DoctorDB.username == doctor_ref))
            .first()
        )
    keys = {str(doctor_ref)}
    if doc:
        keys |= {k for k in (doc.id, doc.email, doc.username) if k}
    return doc, keys


def _bounded_id(raw: str, prefix: str) -> str:
    """Line item ids are VARCHAR(100); fall back to a hashed id if too long."""
    if len(raw) <= 100:
        return raw
    return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:32]}"


def clawback_doctor_payouts_for_case(
    db: Session,
    *,
    case_id: str,
    keep_doctor_keys: Optional[set],
    reason: str,
    period: Optional[str] = None,
    study_ids: Optional[set] = None,
) -> List[dict]:
    """
    For every ACTIVE doctor payout line on the case (optionally only for
    study_ids) whose doctor is NOT in keep_doctor_keys: mark it CLAWED_BACK (+reason) and add one offsetting
    negative line (status CLAWBACK, offsets_line_item_id=<original id>) to that
    doctor's open invoice for the current period. Originals are never deleted
    or changed in amount. Idempotent: a line is only clawed back once.
    Raises ValueError if the target billing period is locked.
    """
    keep = {str(k) for k in (keep_doctor_keys or set()) if k}
    period = period or current_billing_period()
    ts = now_ist_iso()
    event_id = f"cb-{case_id}-{int(time.time() * 1000)}"
    touched_invoices = set()
    results = []
    for li in active_doctor_payout_lines(db, case_id):
        if study_ids is not None and li.study_id not in study_ids:
            continue
        if li.doctor_id and str(li.doctor_id) in keep:
            continue
        already = (
            db.query(InvoiceLineItemDB)
            .filter(InvoiceLineItemDB.offsets_line_item_id == li.id)
            .first()
        )
        if already is None:
            inv = _get_or_create_open_invoice(
                db,
                party_type="doctor",
                party_id=li.doctor_id or "unknown",
                party_name=li.doctor_name or li.doctor_id or "unknown",
                period=period,
            )
            offset = InvoiceLineItemDB(
                id=_bounded_id(f"{li.id}-clawback", "ili-cb"),
                invoice_id=inv.id,
                paired_line_item_id=None,
                billing_event_id=event_id,
                case_id=li.case_id,
                study_id=li.study_id,
                study_index=li.study_index,
                service_date=li.service_date,
                description=f"CLAWBACK of {li.id}: {reason}"[:500],
                modality=li.modality,
                body_part=li.body_part,
                patient_number=li.patient_number,
                patient_name=li.patient_name,
                center_id=li.center_id,
                center_name=li.center_name,
                doctor_id=li.doctor_id,
                doctor_name=li.doctor_name,
                unit_amount=-int(li.unit_amount or 0),
                quantity=int(li.quantity or 1),
                amount=-int(li.amount or 0),
                currency=li.currency or pricing.CURRENCY,
                created_at=ts,
                status=CLAWBACK,
                status_reason=reason,
                offsets_line_item_id=li.id,
            )
            db.add(offset)
            touched_invoices.add(inv.id)
            offset_id = offset.id
        else:
            offset_id = already.id
        li.status = CLAWED_BACK
        li.status_reason = reason
        touched_invoices.add(li.invoice_id)
        results.append(
            {
                "originalLineItemId": li.id,
                "offsetLineItemId": offset_id,
                "doctorId": li.doctor_id,
                "amount": -int(li.amount or 0),
                "reason": reason,
            }
        )
    if results:
        db.flush()
        for inv_id in touched_invoices:
            _recompute_invoice_total(db, inv_id)
        logger.info("Doctor payout clawback for case %s: %s", case_id, results)
    return results


def period_label(period: str) -> str:
    """'2026-09' -> 'September 2026' (falls back to the raw value)."""
    try:
        y, m = str(period).split("-", 1)
        return f"{_calendar.month_name[int(m)]} {int(y)}"
    except Exception:
        return str(period)


def _invoice_period_locked(db: Session, invoice_id: Optional[str]) -> Optional[str]:
    """Return the invoice's billing period if that invoice/period is locked, else None."""
    if not invoice_id:
        return None
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == invoice_id).first()
    if not inv:
        return None
    if inv.locked or is_period_locked(db, inv.billing_period):
        return inv.billing_period
    return None


def locked_periods_blocking_reassign(db: Session, *, case_id: str, target_doctor_ref: str) -> List[str]:
    """
    Read-only pre-check for flag_and_reassign. For an already-billed case, the
    ACTIVE doctor payout lines held by anyone other than the target doctor will
    have to be clawed back when the target re-signs. If any of those lines sit
    on a locked invoice/period, return the (sorted) locked periods; else [].
    Cases not yet billed, or where the target already holds every ACTIVE line
    (A->B->A), return [].
    """
    if not case_already_billed(db, case_id):
        return []
    _doc, target_keys = _doctor_keys(db, target_doctor_ref)
    periods = set()
    for li in active_doctor_payout_lines(db, case_id):
        if li.doctor_id and str(li.doctor_id) in target_keys:
            continue
        locked = _invoice_period_locked(db, li.invoice_id)
        if locked:
            periods.add(locked)
    return sorted(periods)


def payout_locked_message(period: str) -> str:
    return (
        f"Billing for {period_label(period)} is locked, so this case can't be completed yet: "
        f"the previous doctor's payout would have to be reversed in a locked period. "
        f"Ask the Super Admin to unlock {period_label(period)}, then sign again."
    )


def rates_for_center(db: Session, center_id: Optional[str]) -> "pricing.Rates":
    """The centre's rate card (center_pricing row), or the default rates."""
    if not center_id:
        return pricing.DEFAULT_RATES
    row = db.query(CenterPricingDB).filter(CenterPricingDB.center_id == center_id).first()
    if not row:
        return pricing.DEFAULT_RATES
    return pricing.Rates(
        center_first=int(row.center_first_inr),
        center_additional=int(row.center_additional_inr),
        doctor_first=int(row.doctor_first_inr),
        doctor_additional=int(row.doctor_additional_inr),
    )


def _reissue_amount(db: Session, case, study_lines, idx: int) -> int:
    """Payout for a re-signed (reassigned) study: the amount the study's original
    payout was billed at, so a case billed before a rate change stays on the old
    rate. Falls back to the centre's current rate when no original line exists."""
    originals = sorted(
        (li for li in study_lines
         if int(li.amount or 0) > 0 and not getattr(li, "offsets_line_item_id", None)),
        key=lambda li: li.created_at or "",
    )
    if originals:
        return int(originals[0].unit_amount or originals[0].amount or 0)
    return rates_for_center(db, case.radiology_center_id if case else None).doctor_amount(idx)


def ensure_doctor_payout_for_signer(
    db: Session,
    *,
    case_id: str,
    period: Optional[str] = None,
) -> Optional[dict]:
    """
    Called when an already-billed case is fully signed again (the center is never
    re-billed). Works PER STUDY. The clawback of a reassigned case happens here,
    at the new doctor's sign-off, not at reassignment:
      - study's final signer already holds the ACTIVE payout line -> untouched
        (same doctor re-sign, recheck, or A->B->A with B never signing)
      - another doctor holds the ACTIVE line -> claw that line back
        (CLAWED_BACK original + CLAWBACK offset) and issue the signer's line at
        the amount the original payout was billed at (see _reissue_amount)
      - study has no doctor payout history at all (legacy center-only billing)
        -> nothing is issued
    Idempotent: a repeat call finds the signer's ACTIVE line and does nothing.
    Invariant: exactly one ACTIVE doctor payout line per study.
    """
    studies = billing_ordered_studies(db, case_id)
    if not studies:
        return None

    all_lines = _doctor_lines_for_case(db, case_id)
    case = db.query(CaseDB).filter(CaseDB.id == case_id).first()
    center_lines = {
        li.study_id: li
        for li in db.query(InvoiceLineItemDB)
        .join(InvoiceDB, InvoiceDB.id == InvoiceLineItemDB.invoice_id)
        .filter(InvoiceLineItemDB.case_id == case_id, InvoiceDB.party_type == "center")
        .all()
    }
    period = period or current_billing_period()
    date_label = now_ist().strftime("%Y-%m-%d %H:%M IST")
    stamp = int(time.time() * 1000)
    event_id = f"be-{case_id}-{stamp}-reissue"
    ts = now_ist_iso()
    n = len(studies)

    # Safety net (reassign is normally blocked up front under a lock): if any
    # study needs a clawback + re-issue and the target period is locked, fail
    # with a message naming the period instead of another doctor's invoice id.
    # Read-only pre-pass; nothing has been written yet.
    if is_period_locked(db, period):
        for study in studies:
            signer_ref = getattr(study, "signed_by", None) or study.claimed_by
            if signer_ref in (None, "", "ALL"):
                continue
            _d, _keys = _doctor_keys(db, signer_ref)
            s_lines = [li for li in all_lines if li.study_id == study.id]
            if not s_lines:
                continue
            s_active = [
                li for li in s_lines
                if line_item_status(li) == ACTIVE and int(li.amount or 0) > 0
                and not getattr(li, "offsets_line_item_id", None)
            ]
            if not any(li.doctor_id and str(li.doctor_id) in _keys for li in s_active):
                raise ValueError(payout_locked_message(period))

    created, clawed, kept, skipped = [], [], [], []
    touched_invoices = set()
    for idx, study in enumerate(studies, start=1):
        signer_ref = getattr(study, "signed_by", None) or study.claimed_by
        if signer_ref in (None, "", "ALL"):
            skipped.append({"studyId": study.id, "reason": "no_signer"})
            continue
        doc, keys = _doctor_keys(db, signer_ref)
        doctor_id = doc.id if doc else str(signer_ref)
        doctor_name = (doc.full_name if doc else None) or getattr(study, "signed_by_name", None) or study.claimed_by_name or doctor_id

        study_lines = [li for li in all_lines if li.study_id == study.id]
        active = [
            li for li in study_lines
            if line_item_status(li) == ACTIVE and int(li.amount or 0) > 0
            and not getattr(li, "offsets_line_item_id", None)
        ]
        if any(li.doctor_id and str(li.doctor_id) in keys for li in active):
            kept.append({"studyId": study.id, "studyIndex": idx, "doctorId": doctor_id,
                         "reason": "payout_already_active"})
            continue
        if not study_lines:
            skipped.append({"studyId": study.id, "reason": "no_prior_doctor_payout"})
            continue

        # Another doctor holds this study's payout (or it was already reversed):
        # reverse it now that the signer has re-signed this study, then pay the signer.
        clawed += clawback_doctor_payouts_for_case(
            db,
            case_id=case_id,
            keep_doctor_keys=keys,
            reason=f"Case reassigned to {doctor_name}; study re-signed by {doctor_name} on {date_label}",
            period=period,
            study_ids={study.id},
        )
        doctor_inv = _get_or_create_open_invoice(
            db, party_type="doctor", party_id=doctor_id, party_name=doctor_name, period=period
        )
        c_line = center_lines.get(study.id)
        if c_line and not doctor_inv.linked_invoice_id:
            doctor_inv.linked_invoice_id = c_line.invoice_id
        amt = _reissue_amount(db, case, study_lines, idx)
        line_id = _bounded_id(f"ili-d-{case_id}-{idx}-r{stamp}", "ili-d")
        db.add(
            InvoiceLineItemDB(
                id=line_id,
                invoice_id=doctor_inv.id,
                paired_line_item_id=c_line.id if c_line else None,
                billing_event_id=event_id,
                case_id=case_id,
                study_id=study.id,
                study_index=idx,
                service_date=(c_line.service_date if c_line else None)
                or (case.study_date if case else None)
                or now_ist().strftime("%d-%m-%Y"),
                description=f"Payout study {idx}/{n}: {study.body_part} ({study.modality or 'X-Ray'})",
                modality=study.modality,
                body_part=study.body_part,
                patient_number=case.patient_number if case else None,
                patient_name=case.full_name if case else None,
                center_id=case.radiology_center_id if case else None,
                center_name=case.radiology_center_name if case else None,
                doctor_id=doctor_id,
                doctor_name=doctor_name,
                unit_amount=amt,
                quantity=1,
                amount=amt,
                currency=pricing.CURRENCY,
                created_at=ts,
                status=ACTIVE,
                status_reason=None,
                offsets_line_item_id=None,
            )
        )
        touched_invoices.add(doctor_inv.id)
        created.append({"lineId": line_id, "studyId": study.id, "studyIndex": idx,
                        "doctorId": doctor_id, "doctorName": doctor_name, "amount": amt})

    if not created:
        reason = "payout_already_active" if kept else "no_prior_doctor_payout"
        return {"skipped": True, "reason": reason, "kept": kept, "skippedStudies": skipped}

    db.flush()
    for inv_id in touched_invoices:
        _recompute_invoice_total(db, inv_id)
    summary = {
        "doctorLines": created,
        "doctorLineIds": [c["lineId"] for c in created],
        "doctorTotalAdded": sum(c["amount"] for c in created),
        "clawedBack": clawed,
        "kept": kept,
        "skippedStudies": skipped,
    }
    logger.info("Doctor payout issued at sign-off for case %s: %s", case_id, summary)
    return summary


def revenue_summary(db: Session, months: int = 6) -> dict:
    """
    Super Admin revenue overview from invoice tables (real DB totals only).

    Assumption: "net" = sum of center invoice totals minus sum of doctor invoice
    totals for the period (center billing - doctor payouts). Status buckets use
    effective status (paid / pending / overdue).
    """
    months = max(1, min(int(months or 6), 24))
    current = current_billing_period()

    # Distinct periods present, plus ensure current month is included
    periods_q = [
        r[0]
        for r in db.query(InvoiceDB.billing_period).distinct().all()
        if r[0]
    ]
    periods = sorted(set(periods_q) | {current}, reverse=True)

    def summarize_period(period: str) -> dict:
        invoices = db.query(InvoiceDB).filter(InvoiceDB.billing_period == period).all()
        center_billing = 0
        doctor_payouts = 0
        center_paid = 0
        center_unpaid = 0
        doctor_paid = 0
        doctor_unpaid = 0
        center_count = 0
        doctor_count = 0
        for inv in invoices:
            amt = int(inv.total_amount or 0)
            eff = compute_effective_status(inv.status, inv.billing_period)
            is_paid = eff == "paid"
            if inv.party_type == "center":
                center_count += 1
                center_billing += amt
                if is_paid:
                    center_paid += amt
                else:
                    center_unpaid += amt
            elif inv.party_type == "doctor":
                doctor_count += 1
                doctor_payouts += amt
                if is_paid:
                    doctor_paid += amt
                else:
                    doctor_unpaid += amt
        return {
            "period": period,
            "centerBilling": center_billing,
            "doctorPayouts": doctor_payouts,
            "net": center_billing - doctor_payouts,
            "centerPaid": center_paid,
            "centerPending": center_unpaid,
            "doctorPaid": doctor_paid,
            "doctorPending": doctor_unpaid,
            "centerInvoiceCount": center_count,
            "doctorInvoiceCount": doctor_count,
            "currency": pricing.CURRENCY,
        }

    trend = [summarize_period(p) for p in periods[:months]]
    current_month = next((t for t in trend if t["period"] == current), summarize_period(current))

    return {
        "currency": pricing.CURRENCY,
        "currentPeriod": current,
        "currentMonth": current_month,
        "trend": trend,
        "assumption": (
            "Net = center billing total minus doctor payout total for the period. "
            "Paid/pending use effective invoice status (overdue counted with pending unpaid)."
        ),
    }
