from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

try:
    from app.database import get_db
    from app.models import UserDB
    from app.schemas import LoginRequest, LoginResponse, UserResponse, UserCreate
    from app.security import (
        hash_password,
        verify_password,
        looks_like_bcrypt,
        create_access_token,
        user_public_dict,
        get_current_user,
    )
except ImportError:
    from app.database import get_db
    from app.models import UserDB
    from app.schemas import LoginRequest, LoginResponse, UserResponse, UserCreate
    from app.security import (
        hash_password,
        verify_password,
        looks_like_bcrypt,
        create_access_token,
        user_public_dict,
        get_current_user,
    )

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.email.ilike(payload.email)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    meta = dict(user.metadata_ or {})
    stored = meta.get("password") or ""
    if not stored or not verify_password(payload.password, stored):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    # Migrate leftover plaintext (should already be hashed by seed) on successful match is impossible
    # with verify_password rejecting plaintext — seed handles migration.
    token = create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "name": user.name,
        }
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _session_user(db, user),
    }


def _session_user(db: Session, user: UserDB) -> dict:
    """Login / me payload: public user + centre access (multi-centre scoping for the UI)."""
    try:
        try:
            from app import access as access_svc
        except ImportError:
            from app import access as access_svc
        return access_svc.session_dict(db, user)
    except Exception:
        db.rollback()
        return user_public_dict(user)


@router.get("/me")
def get_me(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Current user + centre access; the dashboard refreshes its stored session from this."""
    return _session_user(db, current_user)


try:
    from app.security import require_roles
except ImportError:
    from app.security import require_roles

@router.get("/users", response_model=list[UserResponse])
def get_all_users(
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    users = db.query(UserDB).all()
    return [user_public_dict(u) for u in users]


@router.post("/users", response_model=UserResponse)
def create_or_update_user(
    user: UserCreate,
    db: Session = Depends(get_db),
    _current_user: UserDB = Depends(require_roles("SUPER_ADMIN", "MANAGER")),
):
    existing = db.query(UserDB).filter(UserDB.email.ilike(user.email)).first()
    # A Manager may only manage Doctor / Center logins (no self-promotion, no admin accounts)
    if _current_user.role == "MANAGER":
        if user.role not in ("DOCTOR", "CENTER") or (existing is not None and existing.role not in ("DOCTOR", "CENTER")):
            raise HTTPException(status_code=403, detail="Managers can only manage doctor and centre logins")
    meta_payload = {
        "doctorId": user.doctorId,
        "centerId": user.centerId,
        "avatar": user.avatar,
    }
    if user.password:
        meta_payload["password"] = (
            user.password if looks_like_bcrypt(user.password) else hash_password(user.password)
        )
    if existing:
        existing.name = user.name
        existing.role = user.role
        curr_meta = dict(existing.metadata_ or {})
        curr_meta.update({k: v for k, v in meta_payload.items() if v is not None})
        existing.metadata_ = curr_meta
        flag_modified(existing, "metadata_")
        db.commit()
        db.refresh(existing)
        return user_public_dict(existing)
    else:
        new_u = UserDB(
            email=user.email,
            name=user.name,
            role=user.role,
            metadata_=meta_payload,
        )
        db.add(new_u)
        db.commit()
        db.refresh(new_u)
        return user_public_dict(new_u)
