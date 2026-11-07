"""Read-only preview of open-invoice deletion adjustments.

Does not UPDATE, DELETE, or INSERT. Does not reprice stored line amounts.
Does not touch prepaid wallet ledgers.

Already billed studies keep the rupee amount from sign-off. View-count pricing
applies to new sign-offs. Deleted patients are omitted from open (not paid,
not locked) invoices when those invoices are read; paid and locked invoices
stay as the settlement record.

Take a database backup before the first production read of invoices after
deploy, because that read persists the open-invoice exclusion.

Usage (against a database you choose; this script never writes):

    python scripts/preview_open_invoice_adjustments.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.billing import (  # noqa: E402
    preview_open_invoice_deletion_adjustments,
    preview_view_count_deltas,
)
from app.database import SessionLocal  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        deletions = preview_open_invoice_deletion_adjustments(db)
        deltas = preview_view_count_deltas(db)
        # Prove this process did not flush writes.
        db.rollback()
    finally:
        db.close()
    report = {
        "writes": False,
        "deletionAdjustments": deletions,
        "viewCountDeltasNotApplied": deltas,
        "notes": [
            "action preserve: paid or locked invoice, totals and rows stay.",
            "action exclude_on_read: open invoice will drop these lines from totals and detail rows; the line rows remain with status EXCLUDED_DELETED.",
            "viewCountDeltasNotApplied is informational only. Do not rewrite stored amounts from it.",
            "Prepaid wallet CHARGE rows are not credited by this change.",
        ],
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
