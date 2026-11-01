"""Centre prepaid wallet (t83).

A centre is postpaid (monthly invoices, the default) or prepaid (metadata.billingMode).
Prepaid centres keep a wallet balance in INR:
  * the centre adds money by paying the platform UPI ID and marking "Payment done"
    -> a PENDING top-up request;
  * the Super Admin checks the payment and approves (balance credited) or rejects;
  * each completed case debits the centre charge billed at sign-off (once per case);
  * a new upload is refused while the balance is below one report charge.
Monthly invoices still accrue for every centre (record of charges).

Tables center_wallets / wallet_topups / wallet_transactions are additive and are
created on first use (only when a wallet endpoint is called or a centre is prepaid).

GET  /api/wallet/centers                       SA: every centre with mode + balance
GET  /api/wallet/centers/{id}                  SA / centre login linked to that centre
PUT  /api/wallet/centers/{id}/mode             SA: {"billingMode": "prepaid"|"postpaid"}
POST /api/wallet/centers/{id}/topups           centre admin (or SA): {"amount", "reference"}
GET  /api/wallet/topups?status=PENDING         SA
POST /api/wallet/topups/{id}/approve           SA
POST /api/wallet/topups/{id}/reject            SA: {"note"}
"""
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import re

from app.database import get_db, engine, Base
from app.models import (
    CenterDB,
    UserDB,
    CenterWalletDB,
    WalletTopupDB,
    WalletTransactionDB,
    PaymentCollectionAccountDB,
)
from app.security import get_current_user, require_roles
from app import access as access_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wallet", tags=["Wallet"])

# Env fallback only when no collection account has been selected in the UI yet.
WALLET_UPI_ID = os.getenv("WALLET_UPI_ID", "devanshvala786@okicici")
WALLET_UPI_NAME = os.getenv("WALLET_UPI_NAME", "Radionline")
DEFAULT_TOPUP_INR = 1000
MAX_TOPUP_INR = 500000
LOW_BALANCE_REPORTS = 3  # "running low" warning below this many report charges
UPI_RE = re.compile(r"^[\w.\-]{2,}@[A-Za-z][\w.\-]{1,}$")

_ensured = False


def ensure_wallet_tables(bind=None) -> None:
    global _ensured
    if _ensured and bind is None:
        return
    Base.metadata.create_all(
        bind=bind or engine,
        tables=[
            CenterWalletDB.__table__,
            WalletTopupDB.__table__,
            WalletTransactionDB.__table__,
            PaymentCollectionAccountDB.__table__,
        ],
    )
    _ensured = True


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def billing_mode(center: Optional[CenterDB]) -> str:
    mode = ((center.metadata_ or {}).get("billingMode") if center is not None else None) or "postpaid"
    return "prepaid" if str(mode).lower() == "prepaid" else "postpaid"


def report_charge(db: Session, center_id: str) -> int:
    """One report = the centre's first-study charge (what the cheapest case costs)."""
    try:
        from app import billing as billing_svc
        return int(billing_svc.rates_for_center(db, center_id).center_first_inr)
    except Exception:  # pragma: no cover - fall back to the default rate card
        from app import pricing
        return int(pricing.CENTER_FIRST_STUDY_INR)


def _wallet(db: Session, center_id: str, create: bool = True) -> Optional[CenterWalletDB]:
    ensure_wallet_tables()
    w = db.query(CenterWalletDB).filter(CenterWalletDB.center_id == center_id).first()
    if w is None and create:
        w = CenterWalletDB(center_id=center_id, balance_inr=0, updated_at=_now())
        db.add(w)
        db.flush()
    return w


def _add_txn(db: Session, w: CenterWalletDB, amount: int, kind: str, ref: str, note: str = "") -> WalletTransactionDB:
    w.balance_inr = int(w.balance_inr or 0) + int(amount)
    w.updated_at = _now()
    t = WalletTransactionDB(
        id=f"wtx-{uuid.uuid4().hex[:20]}",
        center_id=w.center_id,
        amount_inr=int(amount),
        kind=kind,
        ref=ref[:150],
        note=(note or "")[:500],
        balance_after=int(w.balance_inr),
        created_at=_now(),
    )
    db.add(t)
    return t


# ---------------------------------------------------------------- hooks used by billing / upload

def assert_can_upload(db: Session, center_id: Optional[str]) -> None:
    """Prepaid centre: refuse a new case while the wallet cannot cover one report (HTTP 402)."""
    if not center_id:
        return
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if billing_mode(center) != "prepaid":
        return
    w = _wallet(db, center_id, create=False)
    bal = int(w.balance_inr or 0) if w else 0
    charge = report_charge(db, center_id)
    if bal < charge:
        raise HTTPException(
            status_code=402,
            detail=f"Wallet balance is too low (\u20b9{bal}; one report costs \u20b9{charge}). Add money to the wallet to upload new cases.",
        )


def debit_for_case(db: Session, case_id: str, center_id: Optional[str], amount: int) -> Optional[dict]:
    """Prepaid centre: debit the centre charge of a completed case, once per case. The balance may go
    below zero (the report was already uploaded while the balance covered it)."""
    if not center_id or not amount:
        return None
    center = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if billing_mode(center) != "prepaid":
        return None
    ensure_wallet_tables()
    ref = f"case:{case_id}"
    if db.query(WalletTransactionDB).filter(WalletTransactionDB.ref == ref, WalletTransactionDB.kind == "CHARGE").first():
        return {"skipped": True, "reason": "already_debited"}
    w = _wallet(db, center_id)
    t = _add_txn(db, w, -abs(int(amount)), "CHARGE", ref, "Report charge")
    logger.info("Wallet debit centre=%s case=%s amount=%s balance=%s", center_id, case_id, amount, w.balance_inr)
    return {"debited": abs(int(amount)), "balance": int(w.balance_inr), "txnId": t.id}


# ---------------------------------------------------------------- collection UPI (QR shown to centres)

def _collection_dict(a: PaymentCollectionAccountDB) -> dict:
    return {
        "id": a.id,
        "label": a.label,
        "upiId": a.upi_id,
        "upiName": a.upi_name,
        "isActive": bool(a.is_active),
        "isSelected": bool(a.is_selected),
        "createdAt": a.created_at,
        "updatedAt": a.updated_at,
    }


def active_collection_upi(db: Session) -> tuple[str, str, Optional[int], Optional[str]]:
    """Return (upiId, upiName, accountId, label) for the Super Admin–selected receive account."""
    ensure_wallet_tables()
    row = (
        db.query(PaymentCollectionAccountDB)
        .filter(PaymentCollectionAccountDB.is_selected == True, PaymentCollectionAccountDB.is_active == True)  # noqa: E712
        .first()
    )
    if row:
        return row.upi_id, row.upi_name or "Radionline", row.id, row.label
    # Fall back to any active account, then env defaults
    any_active = (
        db.query(PaymentCollectionAccountDB)
        .filter(PaymentCollectionAccountDB.is_active == True)  # noqa: E712
        .order_by(PaymentCollectionAccountDB.id.asc())
        .first()
    )
    if any_active:
        return any_active.upi_id, any_active.upi_name or "Radionline", any_active.id, any_active.label
    return WALLET_UPI_ID, WALLET_UPI_NAME, None, None


def _deselect_all(db: Session) -> None:
    for row in db.query(PaymentCollectionAccountDB).filter(PaymentCollectionAccountDB.is_selected == True).all():  # noqa: E712
        row.is_selected = False


# ---------------------------------------------------------------- serialisers

def _topup_dict(t: WalletTopupDB, center_name: Optional[str] = None) -> dict:
    return {
        "id": t.id,
        "centerId": t.center_id,
        "centerName": center_name,
        "amount": int(t.amount_inr or 0),
        "reference": t.reference,
        "status": t.status,
        "requestedBy": t.requested_by,
        "requestedAt": t.requested_at,
        "decidedBy": t.decided_by,
        "decidedAt": t.decided_at,
        "note": t.note,
    }


def _txn_dict(t: WalletTransactionDB) -> dict:
    return {
        "id": t.id, "amount": int(t.amount_inr or 0), "kind": t.kind, "ref": t.ref,
        "note": t.note, "balanceAfter": int(t.balance_after or 0), "createdAt": t.created_at,
    }


def _summary(db: Session, center: CenterDB, detail: bool = True) -> dict:
    ensure_wallet_tables()
    w = _wallet(db, center.id, create=False)
    bal = int(w.balance_inr or 0) if w else 0
    charge = report_charge(db, center.id)
    mode = billing_mode(center)
    out = {
        "centerId": center.id,
        "centerName": center.center_name,
        "billingMode": mode,
        "balance": bal,
        "reportCharge": charge,
        "insufficient": mode == "prepaid" and bal < charge,
        "lowBalance": mode == "prepaid" and bal < charge * LOW_BALANCE_REPORTS,
        "reportsLeft": (bal // charge) if charge > 0 and bal > 0 else 0,
        "defaultTopup": DEFAULT_TOPUP_INR,
    }
    upi_id, upi_name, collection_id, collection_label = active_collection_upi(db)
    out["upiId"] = upi_id
    out["upiName"] = upi_name
    out["collectionAccountId"] = collection_id
    out["collectionLabel"] = collection_label
    if detail:
        tops = (
            db.query(WalletTopupDB).filter(WalletTopupDB.center_id == center.id)
            .order_by(WalletTopupDB.requested_at.desc()).limit(10).all()
        )
        txns = (
            db.query(WalletTransactionDB).filter(WalletTransactionDB.center_id == center.id)
            .order_by(WalletTransactionDB.created_at.desc()).limit(20).all()
        )
        out["topups"] = [_topup_dict(t, center.center_name) for t in tops]
        out["transactions"] = [_txn_dict(t) for t in txns]
    return out


def _center_or_404(db: Session, center_id: str) -> CenterDB:
    c = db.query(CenterDB).filter(CenterDB.id == center_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Centre not found")
    return c


def _require_center_view(db: Session, user: UserDB, center_id: str) -> None:
    if user.role in ("SUPER_ADMIN", "MANAGER"):
        return
    if user.role == "CENTER" and center_id in (access_svc.linked_center_ids(db, user) or []):
        return
    raise HTTPException(status_code=403, detail="Not allowed to view this centre's wallet")


# ---------------------------------------------------------------- endpoints

class ModeIn(BaseModel):
    billingMode: str


class TopupIn(BaseModel):
    amount: int = DEFAULT_TOPUP_INR
    reference: Optional[str] = None


class DecisionIn(BaseModel):
    note: Optional[str] = None


@router.get("/centers")
def list_center_wallets(db: Session = Depends(get_db), _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    try:
        from app.soft_delete import active_filter
    except ImportError:
        from app.soft_delete import active_filter
    centers = active_filter(db.query(CenterDB), CenterDB).order_by(CenterDB.center_name.asc()).all()
    return [_summary(db, c, detail=False) for c in centers]


@router.get("/centers/{center_id}")
def get_center_wallet(center_id: str, db: Session = Depends(get_db), user: UserDB = Depends(get_current_user)):
    _require_center_view(db, user, center_id)
    return _summary(db, _center_or_404(db, center_id))


@router.put("/centers/{center_id}/mode")
def set_billing_mode(center_id: str, body: ModeIn, db: Session = Depends(get_db),
                     _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    mode = (body.billingMode or "").lower()
    if mode not in ("prepaid", "postpaid"):
        raise HTTPException(status_code=400, detail="billingMode must be prepaid or postpaid")
    c = _center_or_404(db, center_id)
    meta = dict(c.metadata_ or {})
    meta["billingMode"] = mode
    c.metadata_ = meta
    if mode == "prepaid":
        _wallet(db, center_id)
    db.commit()
    return _summary(db, c)


@router.post("/centers/{center_id}/topups")
async def request_topup(center_id: str, body: TopupIn, db: Session = Depends(get_db),
                        user: UserDB = Depends(get_current_user)):
    if user.role == "CENTER":
        if center_id not in (access_svc.admin_center_ids(db, user) or []):
            raise HTTPException(status_code=403, detail="Only the centre admin can add money to the wallet")
    elif user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Not allowed")
    c = _center_or_404(db, center_id)
    amount = int(body.amount or 0)
    if amount <= 0 or amount > MAX_TOPUP_INR:
        raise HTTPException(status_code=400, detail=f"Amount must be between 1 and {MAX_TOPUP_INR}")
    ensure_wallet_tables()
    t = WalletTopupDB(
        id=f"wtp-{uuid.uuid4().hex[:20]}",
        center_id=center_id,
        amount_inr=amount,
        reference=((body.reference or "").strip() or None),
        status="PENDING",
        requested_by=(user.name or user.email or str(user.id))[:255],
        requested_at=_now(),
    )
    db.add(t)
    db.commit()
    data = _topup_dict(t, c.center_name)
    try:
        from app.websocket import manager
        await manager.broadcast({"type": "WALLET_TOPUP_REQUESTED", "topup": data})
    except Exception:  # notification is best-effort; SA also polls
        logger.exception("wallet top-up broadcast failed")
    return data


@router.get("/topups")
def list_topups(status: Optional[str] = Query(None), db: Session = Depends(get_db),
                _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    ensure_wallet_tables()
    q = db.query(WalletTopupDB)
    if status:
        q = q.filter(WalletTopupDB.status == status.upper())
    rows = q.order_by(WalletTopupDB.requested_at.desc()).limit(200).all()
    names = {c.id: c.center_name for c in db.query(CenterDB).filter(CenterDB.id.in_({r.center_id for r in rows} or {""})).all()}
    return [_topup_dict(r, names.get(r.center_id)) for r in rows]


def _pending_or_400(db: Session, topup_id: str) -> WalletTopupDB:
    ensure_wallet_tables()
    t = db.query(WalletTopupDB).filter(WalletTopupDB.id == topup_id).with_for_update().first()
    if not t:
        raise HTTPException(status_code=404, detail="Top-up request not found")
    if t.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Top-up request is already {t.status}")
    return t


@router.post("/topups/{topup_id}/approve")
async def approve_topup(topup_id: str, body: Optional[DecisionIn] = None, db: Session = Depends(get_db),
                        user: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    t = _pending_or_400(db, topup_id)
    w = _wallet(db, t.center_id)
    _add_txn(db, w, int(t.amount_inr), "TOPUP", f"topup:{t.id}", (body.note if body else None) or "Top-up approved")
    t.status = "APPROVED"
    t.decided_by = (user.name or "Super Admin")[:255]
    t.decided_at = _now()
    t.note = (body.note if body else None)
    db.commit()
    c = db.query(CenterDB).filter(CenterDB.id == t.center_id).first()
    data = _topup_dict(t, c.center_name if c else None)
    data["balance"] = int(w.balance_inr)
    try:
        from app.websocket import manager
        await manager.broadcast({"type": "WALLET_TOPUP_RESOLVED", "topup": data})
    except Exception:
        logger.exception("wallet top-up broadcast failed")
    return data


@router.post("/topups/{topup_id}/reject")
async def reject_topup(topup_id: str, body: Optional[DecisionIn] = None, db: Session = Depends(get_db),
                       user: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    t = _pending_or_400(db, topup_id)
    t.status = "REJECTED"
    t.decided_by = (user.name or "Super Admin")[:255]
    t.decided_at = _now()
    t.note = (body.note if body else None) or "Payment not received"
    db.commit()
    c = db.query(CenterDB).filter(CenterDB.id == t.center_id).first()
    data = _topup_dict(t, c.center_name if c else None)
    try:
        from app.websocket import manager
        await manager.broadcast({"type": "WALLET_TOPUP_RESOLVED", "topup": data})
    except Exception:
        logger.exception("wallet top-up broadcast failed")
    return data


# ---------------------------------------------------------------- receive-payment accounts (centre QR)

class CollectionAccountIn(BaseModel):
    label: str
    upiId: str
    upiName: Optional[str] = "Radionline"
    isActive: bool = True
    select: bool = False  # if true, make this the account centres pay to


def _apply_collection(row: PaymentCollectionAccountDB, body: CollectionAccountIn) -> None:
    label = " ".join((body.label or "").split())[:120]
    upi = (body.upiId or "").strip()
    name = " ".join((body.upiName or "Radionline").split())[:120] or "Radionline"
    if not label:
        raise HTTPException(status_code=400, detail="Label is required (e.g. Business ICICI)")
    if not UPI_RE.fullmatch(upi):
        raise HTTPException(status_code=400, detail="Invalid UPI ID (e.g. name@okaxis)")
    row.label = label
    row.upi_id = upi
    row.upi_name = name
    row.is_active = bool(body.isActive)
    row.updated_at = _now()


@router.get("/collection-accounts")
def list_collection_accounts(db: Session = Depends(get_db), _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    ensure_wallet_tables()
    rows = (
        db.query(PaymentCollectionAccountDB)
        .order_by(PaymentCollectionAccountDB.is_selected.desc(), PaymentCollectionAccountDB.is_active.desc(), PaymentCollectionAccountDB.id.asc())
        .all()
    )
    # Seed env default once so the UI is never empty on a fresh install
    if not rows and WALLET_UPI_ID:
        seed = PaymentCollectionAccountDB(
            label="Default",
            upi_id=WALLET_UPI_ID,
            upi_name=WALLET_UPI_NAME or "Radionline",
            is_active=True,
            is_selected=True,
            created_at=_now(),
        )
        db.add(seed)
        db.commit()
        db.refresh(seed)
        rows = [seed]
    active = active_collection_upi(db)
    return {
        "accounts": [_collection_dict(r) for r in rows],
        "active": {
            "upiId": active[0],
            "upiName": active[1],
            "id": active[2],
            "label": active[3],
        },
    }


@router.post("/collection-accounts")
def create_collection_account(
    body: CollectionAccountIn,
    db: Session = Depends(get_db),
    _u: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    ensure_wallet_tables()
    row = PaymentCollectionAccountDB(created_at=_now(), is_selected=False)
    _apply_collection(row, body)
    # First account, or explicit select → become the live QR destination
    existing = db.query(PaymentCollectionAccountDB).count()
    if body.select or existing == 0:
        _deselect_all(db)
        row.is_selected = True
        row.is_active = True
    db.add(row)
    db.commit()
    db.refresh(row)
    return _collection_dict(row)


@router.put("/collection-accounts/{account_id}")
def update_collection_account(
    account_id: int,
    body: CollectionAccountIn,
    db: Session = Depends(get_db),
    _u: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    ensure_wallet_tables()
    row = db.query(PaymentCollectionAccountDB).filter(PaymentCollectionAccountDB.id == account_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Collection account not found")
    _apply_collection(row, body)
    if body.select:
        if not row.is_active:
            raise HTTPException(status_code=400, detail="Activate the account before selecting it for the QR")
        _deselect_all(db)
        row.is_selected = True
    elif not body.isActive and row.is_selected:
        row.is_selected = False
    db.commit()
    db.refresh(row)
    return _collection_dict(row)


@router.post("/collection-accounts/{account_id}/select")
def select_collection_account(
    account_id: int,
    db: Session = Depends(get_db),
    _u: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    """Mark this UPI as the one centres see on the Add money QR."""
    ensure_wallet_tables()
    row = db.query(PaymentCollectionAccountDB).filter(PaymentCollectionAccountDB.id == account_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Collection account not found")
    if not row.is_active:
        raise HTTPException(status_code=400, detail="Activate the account before selecting it")
    _deselect_all(db)
    row.is_selected = True
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return _collection_dict(row)
