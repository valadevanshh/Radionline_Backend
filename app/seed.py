import logging
import os

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.models import UserDB
    from app.security import hash_password, looks_like_bcrypt
except ImportError:
    from app.models import UserDB
    from app.security import hash_password, looks_like_bcrypt

logger = logging.getLogger(__name__)


def _ensure_hashed_password(meta: dict) -> tuple[dict, bool]:
    """Return (meta, changed) with password migrated to bcrypt if plaintext."""
    if not meta:
        return meta or {}, False
    pwd = meta.get("password")
    if not pwd or looks_like_bcrypt(pwd):
        return meta, False
    updated = dict(meta)
    updated["password"] = hash_password(pwd)
    return updated, True


def migrate_plaintext_passwords(db: Session):
    """Hash any plaintext passwords stored in users.metadata in place."""
    changed = 0
    users = db.query(UserDB).all()
    for u in users:
        meta = dict(u.metadata_ or {})
        new_meta, did = _ensure_hashed_password(meta)
        if did:
            u.metadata_ = new_meta
            flag_modified(u, "metadata_")
            changed += 1
    if changed:
        db.commit()
        logger.info("Migrated %s user password(s) to bcrypt", changed)


def seed_db(db: Session):
    """Create the first super admin only when the users table is empty.

    Credentials come from BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD.
    Nothing is inserted when either variable is missing or any user already exists.
    """
    existing = db.query(UserDB.id).first()
    email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""
    name = (os.getenv("BOOTSTRAP_ADMIN_NAME") or "Administrator").strip()
    if not existing and email and password:
        db.add(
            UserDB(
                email=email,
                name=name,
                role="SUPER_ADMIN",
                metadata_={"password": hash_password(password)},
            )
        )
        db.commit()
        logger.info("Bootstrapped initial super admin %s", email)
    migrate_plaintext_passwords(db)
