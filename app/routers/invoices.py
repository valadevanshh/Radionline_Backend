"""Invoice & billing period API (JWT + role enforcement)."""
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from app.database import get_db, engine
    from app.models import UserDB, InvoiceDB, InvoiceLineItemDB, BillingPeriodLockDB, CenterDB, CenterPricingDB
    from app.security import get_current_user, require_roles
    from app import billing as billing_svc
    from app import pricing
    from app import access as access_svc
except ImportError:
    from app.database import get_db, engine
    from app.models import UserDB, InvoiceDB, InvoiceLineItemDB, BillingPeriodLockDB, CenterDB, CenterPricingDB
    from app.security import get_current_user, require_roles
    from app import billing as billing_svc
    from app import pricing
    from app import access as access_svc

router = APIRouter(
    prefix="/invoices",
    tags=["Invoices"],
    dependencies=[Depends(get_current_user)],
)

billing_router = APIRouter(
    prefix="/billing",
    tags=["Billing"],
    dependencies=[Depends(get_current_user)],
)


class InvoiceStatusUpdate(BaseModel):
    status: str  # paid | pending


class RecheckToggle(BaseModel):
    include: bool


class LockPeriodRequest(BaseModel):
    period: str  # YYYY-MM
    unlock: Optional[bool] = False


class MarkPeriodPaidRequest(BaseModel):
    period: str  # YYYY-MM
    partyType: Optional[str] = None  # center | doctor | None = both


def _meta(user: UserDB) -> dict:
    return user.metadata_ or {}


@billing_router.get("/pricing")
def get_pricing(
    centerId: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: UserDB = Depends(get_current_user),
):
    """Rate card. A centre account always gets its own centre's rates; Super Admin /
    Manager may pass centerId; everyone else gets the defaults (+ variesByCenter)."""
    if user.role == "CENTER":
        # A centre login gets its own centre's rates: the requested centre if it is
        # linked to it, else its primary centre.
        linked = access_svc.linked_center_ids(db, user) or []
        center_id = centerId if centerId and centerId in linked else _meta(user).get("centerId")
        if center_id not in linked:
            center_id = linked[0] if linked else None
    elif user.role in ("SUPER_ADMIN", "MANAGER"):
        center_id = centerId
    else:
        center_id = None
    out = pricing.pricing_public_dict(billing_svc.rates_for_center(db, center_id))
    out["variesByCenter"] = db.query(CenterPricingDB).first() is not None
    out["doctorRateSource"] = "default"
    if user.role == "DOCTOR":
        personal = billing_svc.explicit_doctor_payout(db, _meta(user).get("doctorId"))
        if personal:
            out["doctor"] = {"firstStudy": personal[0], "additionalStudy": personal[1]}
            out["doctorRateSource"] = "doctor"
            out["isDefault"] = False
    return out


class DoctorPayoutUpdate(BaseModel):
    firstView: int
    additionalView: int


def _doctor_payout_public(db: Session, doc) -> dict:
    personal = billing_svc.explicit_doctor_payout(db, doc.id)
    first, additional = personal or (pricing.DOCTOR_FIRST_STUDY_INR, pricing.DOCTOR_ADDITIONAL_STUDY_INR)
    return {
        "doctorId": doc.id,
        "doctorName": doc.full_name,
        "firstView": first,
        "additionalView": additional,
        "isCustom": personal is not None,
        "source": "doctor" if personal is not None else "default",
    }


@billing_router.get("/doctor-pricing")
def list_doctor_pricing(
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Super Admin: every doctor's payout. No saved tariff means the default 30/15."""
    try:
        from app.models import DoctorDB
        from app.soft_delete import active_filter
    except ImportError:
        from app.models import DoctorDB
        from app.soft_delete import active_filter
    docs = active_filter(db.query(DoctorDB), DoctorDB).order_by(DoctorDB.full_name.asc()).all()
    return {
        "default": {"firstView": pricing.DOCTOR_FIRST_STUDY_INR, "additionalView": pricing.DOCTOR_ADDITIONAL_STUDY_INR},
        "doctors": [_doctor_payout_public(db, doc) for doc in docs],
    }


@billing_router.put("/doctor-pricing/{doctor_id}")
def update_doctor_pricing(
    doctor_id: str,
    body: DoctorPayoutUpdate,
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Super Admin: set one doctor's payout for new sign-offs.

    Values equal to the current default remove the override. Already billed lines stay.
    Managers are not given this route; it matches centre rate cards (Super Admin only).
    """
    try:
        from app.models import DoctorDB
        from app.soft_delete import is_deleted
    except ImportError:
        from app.models import DoctorDB
        from app.soft_delete import is_deleted
    from sqlalchemy.orm.attributes import flag_modified

    doc = db.query(DoctorDB).filter(DoctorDB.id == doctor_id).first()
    if not doc or is_deleted(doc):
        raise HTTPException(status_code=404, detail="Doctor not found")
    values = [body.firstView, body.additionalView]
    if any((not isinstance(v, int)) or v < 0 or v > pricing.MAX_RATE_INR for v in values):
        raise HTTPException(
            status_code=422,
            detail=f"Each rate must be a whole number of rupees from 0 to {pricing.MAX_RATE_INR}.",
        )
    meta = dict(doc.metadata_ or {})
    if values == [pricing.DOCTOR_FIRST_STUDY_INR, pricing.DOCTOR_ADDITIONAL_STUDY_INR]:
        meta.pop("payoutRates", None)
    else:
        meta["payoutRates"] = {"firstView": body.firstView, "additionalView": body.additionalView}
    doc.metadata_ = meta
    flag_modified(doc, "metadata_")
    db.commit()
    billing_svc.logger.info(
        "Doctor payout changed: doctor=%s by=%s first=%s additional=%s",
        doctor_id, user.email, body.firstView, body.additionalView,
    )
    return _doctor_payout_public(db, doc)


class CenterPricingUpdate(BaseModel):
    centerFirstStudy: int
    centerAdditionalStudy: int
    doctorFirstStudy: int
    doctorAdditionalStudy: int


def _center_pricing_row(center: CenterDB, rates) -> dict:
    return {
        "centerId": center.id,
        "centerName": center.center_name,
        "currency": pricing.CURRENCY,
        **rates.as_dict(),
        "isDefault": rates.is_default,
    }


@billing_router.get("/center-pricing")
def list_center_pricing(
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Super Admin: every centre with its effective rate card (defaults where unset)."""
    rows = {r.center_id: r for r in db.query(CenterPricingDB).all()}
    out = []
    for c in db.query(CenterDB).all():
        r = rows.get(c.id)
        item = _center_pricing_row(c, billing_svc.rates_for_center(db, c.id))
        item["updatedAt"] = r.updated_at if r else None
        item["updatedBy"] = r.updated_by if r else None
        out.append(item)
    return {"default": pricing.DEFAULT_RATES.as_dict(), "centers": out}


@billing_router.put("/center-pricing/{center_id}")
def update_center_pricing(
    center_id: str,
    body: CenterPricingUpdate,
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Super Admin: set a centre's rates. Applies to cases signed from now on;
    already billed line items keep their amounts. Default values = no row."""
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not center:
        raise HTTPException(status_code=404, detail="Center not found")
    values = [body.centerFirstStudy, body.centerAdditionalStudy, body.doctorFirstStudy, body.doctorAdditionalStudy]
    if any(v < 0 or v > pricing.MAX_RATE_INR for v in values):
        raise HTTPException(
            status_code=422,
            detail=f"Each rate must be a whole number of rupees from 0 to {pricing.MAX_RATE_INR}.",
        )
    new_rates = pricing.Rates(*values)
    old_rates = billing_svc.rates_for_center(db, center_id)
    row = db.query(CenterPricingDB).filter(CenterPricingDB.center_id == center_id).first()
    if new_rates.is_default:
        # Back to the default rate card: drop the override so the centre follows defaults.
        if row:
            db.delete(row)
        row = None
    else:
        if row is None:
            row = CenterPricingDB(center_id=center_id)
            db.add(row)
        row.center_first_inr = new_rates.center_first
        row.center_additional_inr = new_rates.center_additional
        row.doctor_first_inr = new_rates.doctor_first
        row.doctor_additional_inr = new_rates.doctor_additional
        row.updated_at = billing_svc.now_ist_iso()
        row.updated_by = user.email
    db.commit()
    billing_svc.logger.info(
        "Center pricing changed: center=%s by=%s old=%s new=%s",
        center_id, user.email, old_rates.as_dict(), new_rates.as_dict(),
    )
    item = _center_pricing_row(center, new_rates)
    item["updatedAt"] = row.updated_at if row else None
    item["updatedBy"] = row.updated_by if row else None
    return item


@billing_router.get("/revenue")
def get_revenue_summary(
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Super Admin only: monthly revenue from Priority 3 invoice tables (real DB sums)."""
    result = billing_svc.revenue_summary(db, months=months)
    db.commit()
    return result


@billing_router.get("/periods")
def list_periods(
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    locks = db.query(BillingPeriodLockDB).order_by(BillingPeriodLockDB.period.desc()).all()
    # Also include periods that have invoices but no lock row yet
    periods_from_inv = [
        r[0]
        for r in db.query(InvoiceDB.billing_period).distinct().order_by(InvoiceDB.billing_period.desc()).all()
    ]
    lock_map = {l.period: l for l in locks}
    all_periods = sorted(set(list(lock_map.keys()) + periods_from_inv), reverse=True)
    out = []
    for p in all_periods:
        l = lock_map.get(p)
        out.append({
            "period": p,
            "locked": bool(l.locked) if l else False,
            "lockedAt": l.locked_at if l else None,
            "lockedBy": l.locked_by if l else None,
            "effectiveStatusHint": billing_svc.compute_effective_status("pending", p),
        })
    return out


@billing_router.post("/lock-period")
def lock_period(
    body: LockPeriodRequest,
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    try:
        if body.unlock:
            result = billing_svc.unlock_billing_period(db, body.period, user.name or user.email)
        else:
            result = billing_svc.lock_billing_period(db, body.period, user.name or user.email)
        db.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@billing_router.post("/mark-period-paid")
def mark_period_paid(
    body: MarkPeriodPaidRequest,
    db: Session = Depends(get_db),
    _user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Mark every invoice in a month as paid (optional partyType filter)."""
    try:
        result = billing_svc.mark_period_paid(db, body.period, body.partyType)
        db.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
def list_invoices(
    party_type: Optional[str] = Query(None, alias="partyType"),
    party_id: Optional[str] = Query(None, alias="partyId"),
    period: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
    user: UserDB = Depends(get_current_user),
):
    q = db.query(InvoiceDB)
    meta = _meta(user)

    if user.role == "CENTER":
        cids = access_svc.center_ids_with(db, user, "invoices", "read") or []
        if not cids:
            raise HTTPException(status_code=403, detail="You do not have access to invoices")
        if party_id:
            if party_id not in cids:
                raise HTTPException(status_code=403, detail="You do not have access to this centre's invoices")
            cids = [party_id]
        q = q.filter(InvoiceDB.party_type == "center", InvoiceDB.party_id.in_(cids))
    elif user.role == "DOCTOR":
        did = meta.get("doctorId")
        if not did:
            raise HTTPException(status_code=403, detail="Doctor account missing doctorId")
        q = q.filter(InvoiceDB.party_type == "doctor", InvoiceDB.party_id == did)
    elif user.role == "SUPER_ADMIN":
        if party_type:
            q = q.filter(InvoiceDB.party_type == party_type)
        if party_id:
            q = q.filter(InvoiceDB.party_id == party_id)
    elif user.role == "MANAGER":
        # Managers can view center invoices only (read-only via this list)
        if party_type:
            q = q.filter(InvoiceDB.party_type == party_type)
        else:
            q = q.filter(InvoiceDB.party_type == "center")
        if party_id:
            q = q.filter(InvoiceDB.party_id == party_id)
    else:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if period:
        q = q.filter(InvoiceDB.billing_period == period)

    invoices = q.order_by(InvoiceDB.billing_period.desc(), InvoiceDB.party_name.asc()).all()
    if _sync_deleted_patients(db, invoices):
        db.commit()
    result = [billing_svc.invoice_to_dict(inv) for inv in invoices]
    if status_filter:
        sf = status_filter.lower()
        result = [r for r in result if r["status"] == sf]
    return result


@router.get("/{invoice_id}")
def get_invoice(
    invoice_id: str,
    db: Session = Depends(get_db),
    user: UserDB = Depends(get_current_user),
):
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _authorize_invoice_read(user, inv, db)
    if _sync_deleted_patients(db, [inv]):
        db.commit()
        db.refresh(inv)

    lines = (
        db.query(InvoiceLineItemDB)
        .filter(InvoiceLineItemDB.invoice_id == invoice_id)
        .order_by(InvoiceLineItemDB.service_date.asc(), InvoiceLineItemDB.study_index.asc())
        .all()
    )
    data = billing_svc.invoice_to_dict(inv)
    # t83: "Add Recheck amount" - reassigned-away payouts (CLAWBACK offsets) are listed for the
    # Super Admin; when included they are left out of the counted lines, totals and exports.
    rc = billing_svc.recheck_lines(lines)
    data["recheckLines"] = [billing_svc.line_item_to_dict(li) for li in rc]
    data["recheckAmount"] = sum(-int(li.amount or 0) for li in rc)
    shown = billing_svc.counted_lines(inv, lines)
    data["lineItems"] = [billing_svc.line_item_to_dict(li) for li in shown]
    data["byDay"] = billing_svc.group_lines_by_day(shown)
    if inv.party_type == "doctor":
        data["byCenter"] = billing_svc.group_lines_by_center(shown)
    return data


@router.patch("/{invoice_id}/recheck")
def set_invoice_recheck(
    invoice_id: str,
    body: RecheckToggle,
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """t83 (Super Admin): include / exclude the reassigned-away (recheck) payouts on a doctor invoice."""
    inv = db.query(InvoiceDB).filter(InvoiceDB.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.party_type != "doctor":
        raise HTTPException(status_code=400, detail="Recheck amount applies to doctor invoices only")
    if inv.locked:
        raise HTTPException(status_code=409, detail="This invoice's month is locked; unlock it to change the recheck amount")
    meta = dict(inv.metadata_ or {})
    meta["includeRecheck"] = bool(body.include)
    inv.metadata_ = meta
    db.flush()
    billing_svc._recompute_invoice_total(db, inv.id)
    db.commit()
    return get_invoice(invoice_id, db, user)


@router.patch("/{invoice_id}/status")
def update_invoice_status(
    invoice_id: str,
    body: InvoiceStatusUpdate,
    db: Session = Depends(get_db),
    user: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    try:
        inv = billing_svc.set_invoice_status(db, invoice_id, body.status)
        db.commit()
        db.refresh(inv)
        return billing_svc.invoice_to_dict(inv)
    except KeyError:
        raise HTTPException(status_code=404, detail="Invoice not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _sync_deleted_patients(db: Session, invoices: List[InvoiceDB]) -> bool:
    """Persist open-invoice deletion adjustments. Paid and locked invoices are skipped."""
    changed = False
    for inv in invoices:
        if billing_svc.sync_open_invoice_deletions(db, inv):
            changed = True
    return changed


def _authorize_invoice_read(user: UserDB, inv: InvoiceDB, db: Optional[Session] = None) -> None:
    meta = _meta(user)
    if user.role == "SUPER_ADMIN":
        return
    if user.role == "MANAGER" and inv.party_type == "center":
        return
    if user.role == "CENTER" and inv.party_type == "center" and db is not None and access_svc.has_center_perm(
        db, user, inv.party_id, "invoices", "read"
    ):
        return
    if user.role == "DOCTOR" and inv.party_type == "doctor" and inv.party_id == meta.get("doctorId"):
        return
    raise HTTPException(status_code=403, detail="Not allowed to view this invoice")
