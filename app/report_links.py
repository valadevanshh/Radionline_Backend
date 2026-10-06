"""Public, read-only report links: one unguessable token per signed study report."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

try:
    from app.config import settings
    from app.models import Base, ReportPublicLinkDB, StudyDB
except ImportError:
    from app.config import settings
    from app.models import Base, ReportPublicLinkDB, StudyDB

logger = logging.getLogger(__name__)


def ensure_report_link_tables(engine) -> None:
    """Create report_public_links if missing (Postgres + SQLite). Additive only."""
    Base.metadata.create_all(bind=engine, tables=[ReportPublicLinkDB.__table__])
    logger.info("Ensured table exists: report_public_links")


def new_token() -> str:
    # 32 random bytes from the OS CSPRNG -> 43 URL-safe characters
    return secrets.token_urlsafe(32)


def active_link_for_study(db: Session, study_id: str) -> Optional[ReportPublicLinkDB]:
    return (
        db.query(ReportPublicLinkDB)
        .filter(ReportPublicLinkDB.study_id == study_id, ReportPublicLinkDB.revoked_at.is_(None))
        .order_by(ReportPublicLinkDB.created_at.asc())
        .first()
    )


def ensure_public_link(db: Session, study: StudyDB) -> str:
    """Return the study's active token, creating one at first signing. A re-sign keeps
    the same token, so QR codes already printed keep working. Caller commits."""
    link = active_link_for_study(db, study.id)
    if link is None:
        link = ReportPublicLinkDB(
            token=new_token(),
            case_id=study.case_id,
            study_id=study.id,
            created_at=datetime.utcnow().isoformat() + "Z",
        )
        db.add(link)
    meta = dict(study.metadata_ or {})
    meta["publicToken"] = link.token
    study.metadata_ = meta
    return link.token


def revoke_public_link(db: Session, study: StudyDB) -> bool:
    """Revoke the active link (Super Admin). The next sign-off issues a new token."""
    link = active_link_for_study(db, study.id)
    if link is None:
        return False
    link.revoked_at = datetime.utcnow().isoformat() + "Z"
    meta = dict(study.metadata_ or {})
    meta.pop("publicToken", None)
    study.metadata_ = meta
    return True


def find_active_link(db: Session, token: str) -> Optional[ReportPublicLinkDB]:
    tok = (token or "").strip()
    if not tok or len(tok) > 64:
        return None
    return (
        db.query(ReportPublicLinkDB)
        .filter(ReportPublicLinkDB.token == tok, ReportPublicLinkDB.revoked_at.is_(None))
        .first()
    )


def public_report_url(token: Optional[str]) -> Optional[str]:
    """Absolute link when PUBLIC_REPORT_BASE_URL is set; otherwise None and the
    frontend uses its own origin."""
    if not token or not settings.PUBLIC_REPORT_BASE_URL:
        return None
    return f"{settings.PUBLIC_REPORT_BASE_URL}/r/{token}"
