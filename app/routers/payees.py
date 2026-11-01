"""Super Admin: payout account details (bank / UPI) of people we pay.

GET    /api/payees              list (optional ?type=REFERRER|DOCTOR|OTHER&active=true)
POST   /api/payees              create
PUT    /api/payees/{id}         update (send isActive=false to deactivate)
Stored in the additive payee_accounts table, created on first use.
"""
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db, engine, Base
from app.models import PayeeAccountDB, CenterDB, DoctorDB, UserDB
from app.security import require_roles

router = APIRouter(prefix="/payees", tags=["Payees"])

TYPES = ("REFERRER", "DOCTOR", "OTHER")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_ensured = False


def ensure_payee_table(bind=None) -> None:
    global _ensured
    if _ensured and bind is None:
        return
    Base.metadata.create_all(bind=bind or engine, tables=[PayeeAccountDB.__table__])
    _ensured = True


class PayeeIn(BaseModel):
    payeeName: str
    payeeType: str = "OTHER"
    centerId: Optional[str] = None
    doctorId: Optional[str] = None
    accountHolder: Optional[str] = None
    bankName: Optional[str] = None
    accountNumber: Optional[str] = None
    ifsc: Optional[str] = None
    upiId: Optional[str] = None
    notes: Optional[str] = None
    isActive: bool = True


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clean(v: Optional[str], n: int) -> Optional[str]:
    v = " ".join((v or "").split())[:n]
    return v or None


def _to_schema(p: PayeeAccountDB, centers: dict, doctors: dict) -> dict:
    return {
        "id": p.id,
        "payeeName": p.payee_name,
        "payeeType": p.payee_type,
        "centerId": p.center_id,
        "centerName": centers.get(p.center_id) if p.center_id else None,
        "doctorId": p.doctor_id,
        "doctorName": doctors.get(p.doctor_id) if p.doctor_id else None,
        "accountHolder": p.account_holder,
        "bankName": p.bank_name,
        "accountNumber": p.account_number,
        "ifsc": p.ifsc,
        "upiId": p.upi_id,
        "notes": p.notes,
        "isActive": bool(p.is_active),
        "createdAt": p.created_at,
        "updatedAt": p.updated_at,
    }


def _names(db: Session):
    centers = {c.id: c.center_name for c in db.query(CenterDB.id, CenterDB.center_name).all()}
    doctors = {d.id: d.full_name for d in db.query(DoctorDB.id, DoctorDB.full_name).all()}
    return centers, doctors


def _apply(p: PayeeAccountDB, body: PayeeIn, db: Session) -> None:
    name = _clean(body.payeeName, 255)
    if not name:
        raise HTTPException(status_code=400, detail="Payee name is required")
    ptype = (body.payeeType or "OTHER").upper()
    if ptype not in TYPES:
        raise HTTPException(status_code=400, detail="Type must be Referring doctor, Radiologist or Other")
    center_id = _clean(body.centerId, 100) if ptype == "REFERRER" else None
    doctor_id = _clean(body.doctorId, 100) if ptype == "DOCTOR" else None
    if center_id and not db.query(CenterDB.id).filter(CenterDB.id == center_id).first():
        raise HTTPException(status_code=400, detail="Centre not found")
    if doctor_id and not db.query(DoctorDB.id).filter(DoctorDB.id == doctor_id).first():
        raise HTTPException(status_code=400, detail="Radiologist not found")
    acct = re.sub(r"\s+", "", body.accountNumber or "")
    if acct and not re.fullmatch(r"\d{6,18}", acct):
        raise HTTPException(status_code=400, detail="Account number must be 6-18 digits")
    ifsc = (body.ifsc or "").strip().upper()
    if ifsc and not IFSC_RE.fullmatch(ifsc):
        raise HTTPException(status_code=400, detail="Invalid IFSC (e.g. SBIN0001234)")
    upi = (body.upiId or "").strip()
    if upi and not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z][\w.\-]{1,}", upi):
        raise HTTPException(status_code=400, detail="Invalid UPI ID (e.g. name@okaxis)")
    if not acct and not upi:
        raise HTTPException(status_code=400, detail="Enter a bank account or a UPI ID")
    if acct and not ifsc:
        raise HTTPException(status_code=400, detail="IFSC is required with a bank account")
    p.payee_name = name.upper()
    p.payee_type = ptype
    p.center_id = center_id
    p.doctor_id = doctor_id
    p.account_holder = _clean(body.accountHolder, 255)
    p.bank_name = _clean(body.bankName, 255)
    p.account_number = acct or None
    p.ifsc = ifsc or None
    p.upi_id = upi or None
    p.notes = (body.notes or "").strip()[:1000] or None
    p.is_active = bool(body.isActive)


@router.get("")
def list_payees(
    type: Optional[str] = None,
    active: Optional[bool] = None,
    db: Session = Depends(get_db),
    _u: UserDB = Depends(require_roles("SUPER_ADMIN")),
):
    ensure_payee_table()
    q = db.query(PayeeAccountDB)
    if type:
        q = q.filter(PayeeAccountDB.payee_type == type.upper())
    if active is not None:
        q = q.filter(PayeeAccountDB.is_active == active)
    centers, doctors = _names(db)
    return [_to_schema(p, centers, doctors) for p in q.order_by(PayeeAccountDB.is_active.desc(), PayeeAccountDB.payee_name.asc()).all()]


@router.post("")
def create_payee(body: PayeeIn, db: Session = Depends(get_db), _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    ensure_payee_table()
    p = PayeeAccountDB(created_at=_now())
    _apply(p, body, db)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _to_schema(p, *_names(db))


@router.put("/{payee_id}")
def update_payee(payee_id: int, body: PayeeIn, db: Session = Depends(get_db), _u: UserDB = Depends(require_roles("SUPER_ADMIN"))):
    ensure_payee_table()
    p = db.query(PayeeAccountDB).filter(PayeeAccountDB.id == payee_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Payee not found")
    _apply(p, body, db)
    p.updated_at = _now()
    db.commit()
    db.refresh(p)
    return _to_schema(p, *_names(db))
