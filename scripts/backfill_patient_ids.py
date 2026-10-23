"""OPTIONAL one-off backfill: give old cases with no Patient ID (empty or a raw UUID) a random
7-digit Patient ID. Cases that already have a typed value (e.g. '26/1647') are left alone.

Dry run by default (prints what it would change, writes nothing):
    python scripts/backfill_patient_ids.py
Apply (writes cases.patient_number and the matching legacy xray_reports row):
    python scripts/backfill_patient_ids.py --apply
Take a DB backup first. Not run automatically by deploy.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.models import CaseDB, ReportDB  # noqa: E402
from app.case_ops import _UUID_RE, generate_patient_number  # noqa: E402


def main(apply: bool) -> None:
    db = SessionLocal()
    try:
        todo = [c for c in db.query(CaseDB).all() if not (c.patient_number or "").strip() or _UUID_RE.match((c.patient_number or "").strip())]
        print(f"{len(todo)} case(s) without a Patient ID")
        for c in todo:
            new = generate_patient_number(db)
            print(f"  {c.id}  {c.full_name!r}  {c.patient_number!r} -> {new}")
            if apply:
                c.patient_number = new
                rep = db.query(ReportDB).filter(ReportDB.id == c.id).first()
                if rep is not None:
                    rep.patient_number = new
                db.flush()  # so the next uniqueness check sees this value
        if apply:
            db.commit()
            print("Applied.")
        else:
            db.rollback()
            print("Dry run only. Re-run with --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])