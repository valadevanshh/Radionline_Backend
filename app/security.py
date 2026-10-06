import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

try:
    from app.config import settings
    from app.database import get_db
    from app.models import UserDB
except ImportError:
    from app.config import settings
    from app.database import get_db
    from app.models import UserDB

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed_or_plain: str) -> bool:
    """Verify password against bcrypt hash. Rejects plaintext stored values."""
    if not hashed_or_plain:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed_or_plain.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def looks_like_bcrypt(value: str) -> bool:
    return isinstance(value, str) and value.startswith("") and len(value) >= 50


def create_access_token(data: dict[str, Any], expires_hours: Optional[int] = None) -> str:
    payload = data.copy()
    hours = expires_hours if expires_hours is not None else settings.JWT_EXPIRE_HOURS
    expire = datetime.now(timezone.utc) + timedelta(hours=hours)
    payload["exp"] = expire
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def user_public_dict(u: UserDB) -> dict:
    meta = u.metadata_ or {}
    return {
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "doctorId": meta.get("doctorId"),
        "centerId": meta.get("centerId"),
        "avatar": meta.get("avatar"),
    }


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> UserDB:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.query(UserDB).filter(UserDB.id == uid).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Prefer role from DB; token role is informational
    return user


def require_roles(*roles: str):
    def _checker(current_user: UserDB = Depends(get_current_user)) -> UserDB:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user

    return _checker
