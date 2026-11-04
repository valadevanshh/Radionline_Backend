"""Manager accounts: per-page permissions chosen by the Super Admin.

A Manager (role MANAGER) has, for each page, three switches:
    read   -> may open the page / list its data (applies immediately)
    write  -> may *request* create / edit (staged for Super Admin approval)
    delete -> may *request* delete (staged for Super Admin approval)

Stored in users.metadata.managerPermissions = {page: {"read", "write", "delete"}}.
A Manager created before this feature (no managerPermissions key) keeps full
access so existing accounts keep working until the Super Admin edits them.
Write or delete implies read.
"""
from typing import Dict, Optional

from fastapi import HTTPException, status

MANAGER_PAGES = ("reports", "doctors", "centers")
ACTIONS = ("read", "write", "delete")
PAGE_LABELS = {"reports": "All Reports", "doctors": "Doctors", "centers": "Centers"}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def normalize_manager_permissions(raw) -> Dict[str, Dict[str, bool]]:
    """Every page present with read/write/delete booleans; write/delete imply read."""
    src = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, bool]] = {}
    for page in MANAGER_PAGES:
        p = src.get(page)
        if isinstance(p, str):  # tolerate "none" | "read" | "write" | "delete" shorthand
            lv = p.strip().lower()
            p = {"read": lv in ("read", "write", "delete"), "write": lv in ("write", "delete"), "delete": lv == "delete"}
        p = p if isinstance(p, dict) else {}
        entry = {a: _truthy(p.get(a)) for a in ACTIONS}
        if entry["write"] or entry["delete"]:
            entry["read"] = True
        out[page] = entry
    return out


FULL_MANAGER_PERMISSIONS = {p: {a: True for a in ACTIONS} for p in MANAGER_PAGES}


def manager_permissions(user) -> Optional[Dict[str, Dict[str, bool]]]:
    """None for non-managers (not restricted by this module)."""
    if user is None or getattr(user, "role", None) != "MANAGER":
        return None
    meta = user.metadata_ or {}
    if "managerPermissions" not in meta:
        return {p: dict(v) for p, v in FULL_MANAGER_PERMISSIONS.items()}
    return normalize_manager_permissions(meta.get("managerPermissions"))


def manager_can(user, page: str, action: str = "read") -> bool:
    perms = manager_permissions(user)
    if perms is None:
        return True
    return bool(perms.get(page, {}).get(action))


def require_manager_perm(user, page: str, action: str = "read") -> None:
    """No-op for every role except MANAGER."""
    if not manager_can(user, page, action):
        verb = {"read": "view", "write": "add or edit", "delete": "delete"}.get(action, action)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your Manager account is not allowed to {verb} on {PAGE_LABELS.get(page, page)}",
        )


def optional_current_user(request, db):
    """The logged-in user when a valid bearer token is sent, else None (never raises)."""
    try:
        from app.security import decode_access_token
        from app.models import UserDB
    except ImportError:  # pragma: no cover
        return None
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    try:
        payload = decode_access_token(auth.split(" ", 1)[1].strip())
        uid = int(payload.get("sub"))
    except Exception:
        return None
    return db.query(UserDB).filter(UserDB.id == uid).first()
