"""Read-only note for the doctor payout default change (20/10 -> 30/15).

Does not UPDATE, DELETE, or INSERT. Does not reprice signed, paid, or historical lines.

What changes in code:
  Doctors with no metadata.payoutRates, signing at a centre with no center_pricing row,
  are paid 30 for the first view and 15 for each additional view on NEW sign-offs.

What is left alone:
  center_pricing.doctor_first_inr / doctor_additional_inr, including rows still stored
  as 20/10. Those stay the centre's doctor payout for doctors with no saved tariff.
  A doctor's metadata.payoutRates, when present, stays and wins over the centre,
  including an explicit 30/15 or 0. Only a separate clear removes it.
  Invoice line amounts already written are not modified.

Take a database backup before deploy. This script only prints.

    python scripts/preview_doctor_default_rates.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models import CenterPricingDB, DoctorDB  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        centres = []
        for row in db.query(CenterPricingDB).order_by(CenterPricingDB.center_id.asc()).all():
            centres.append({
                "centerId": row.center_id,
                "doctorFirst": int(row.doctor_first_inr),
                "doctorAdditional": int(row.doctor_additional_inr),
                "action": "keep",
                "reason": "explicit centre doctor payout; not rewritten",
            })
        doctors = []
        for doc in db.query(DoctorDB).order_by(DoctorDB.full_name.asc()).all():
            raw = (doc.metadata_ or {}).get("payoutRates")
            doctors.append({
                "doctorId": doc.id,
                "hasSavedTariff": isinstance(raw, dict) and "firstView" in raw and "additionalView" in raw,
                "saved": raw if isinstance(raw, dict) else None,
                "action": "keep" if isinstance(raw, dict) else "use-new-default-on-future-signoff",
            })
        db.rollback()
    finally:
        db.close()
    json.dump({
        "writes": False,
        "newDefault": {"firstView": 30, "additionalView": 15},
        "previousCodeDefault": {"firstView": 20, "additionalView": 10},
        "centreDoctorRatesKept": centres,
        "doctors": doctors,
    }, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
