"""Persistent PACS image marks (arrows) per case image.

Anyone who can open the case sees the marks; doctors with access to the case
and Super Admins can add them; a mark can be removed by its author or a Super Admin.
Coordinates are normalised to the image (0..1000 on both axes), so a mark stays
on the same anatomy at any zoom, pan, rotation, flip or screen size."""
import math
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from app.database import get_db
    from app.models import ImageAnnotationDB, UserDB
    from app.security import get_current_user
    from app.websocket import manager as ws_manager
    from app.routers.case_thread import _resolve_case, _can_access_case
except ImportError:
    from app.database import get_db
    from app.models import ImageAnnotationDB, UserDB
    from app.security import get_current_user
    from app.websocket import manager as ws_manager
    from app.routers.case_thread import _resolve_case, _can_access_case

router = APIRouter(prefix="/reports", tags=["Image annotations"])

DRAW_ROLES = ("DOCTOR", "SUPER_ADMIN")
KINDS = ("arrow",)
MAX_PER_IMAGE = 200


class AnnotationCreate(BaseModel):
    imageKey: str
    kind: str = "arrow"
    data: Dict[str, float]


def _to_schema(a: ImageAnnotationDB) -> dict:
    return {
        "id": a.id,
        "caseId": a.case_id,
        "imageKey": a.image_key,
        "kind": a.kind,
        "data": a.data or {},
        "authorUserId": a.author_user_id,
        "authorName": a.author_name,
        "authorRole": a.author_role,
        "createdAt": a.created_at,
    }


def _case_for(db: Session, user: UserDB, report_id: str):
    case = _resolve_case(db, report_id)
    if not _can_access_case(db, user, case):
        raise HTTPException(status_code=403, detail="Not allowed to access this case")
    return case


def _clean_arrow(data: Dict[str, float]) -> Dict[str, float]:
    out = {}
    for k in ("x1", "y1", "x2", "y2"):
        v = data.get(k)
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Arrow needs numeric {k}")
        if not math.isfinite(f):
            raise HTTPException(status_code=400, detail=f"Arrow needs numeric {k}")
        out[k] = round(min(1000.0, max(0.0, f)), 2)
    if math.hypot(out["x2"] - out["x1"], out["y2"] - out["y1"]) < 5:
        raise HTTPException(status_code=400, detail="Arrow is too short")
    return out


@router.get("/{report_id}/annotations")
def list_annotations(report_id: str, db: Session = Depends(get_db),
                     current_user: UserDB = Depends(get_current_user)):
    case = _case_for(db, current_user, report_id)
    rows = (db.query(ImageAnnotationDB).filter(ImageAnnotationDB.case_id == case.id)
            .order_by(ImageAnnotationDB.id.asc()).all())
    return [_to_schema(r) for r in rows]


@router.post("/{report_id}/annotations")
async def add_annotation(report_id: str, body: AnnotationCreate, db: Session = Depends(get_db),
                         current_user: UserDB = Depends(get_current_user)):
    if current_user.role not in DRAW_ROLES:
        raise HTTPException(status_code=403, detail="Only doctors can mark images")
    case = _case_for(db, current_user, report_id)
    kind = (body.kind or "arrow").strip().lower()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Unsupported mark type")
    key = (body.imageKey or "").strip()
    if not key or len(key) > 500:
        raise HTTPException(status_code=400, detail="imageKey is required")
    if db.query(ImageAnnotationDB).filter(ImageAnnotationDB.case_id == case.id,
                                          ImageAnnotationDB.image_key == key).count() >= MAX_PER_IMAGE:
        raise HTTPException(status_code=400, detail="Too many marks on this image")
    row = ImageAnnotationDB(
        case_id=case.id, image_key=key, kind=kind, data=_clean_arrow(body.data or {}),
        author_user_id=current_user.id, author_name=current_user.name, author_role=current_user.role,
        created_at=datetime.utcnow().isoformat() + "Z",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    out = _to_schema(row)
    await ws_manager.broadcast({"type": "CASE_ANNOTATIONS_CHANGED", "caseId": case.id, "action": "added", "annotation": out})
    return out


@router.delete("/{report_id}/annotations/{annotation_id}")
async def delete_annotation(report_id: str, annotation_id: int, db: Session = Depends(get_db),
                            current_user: UserDB = Depends(get_current_user)):
    case = _case_for(db, current_user, report_id)
    row = db.query(ImageAnnotationDB).filter(ImageAnnotationDB.id == annotation_id,
                                             ImageAnnotationDB.case_id == case.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Mark not found")
    if current_user.role != "SUPER_ADMIN" and row.author_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the doctor who drew this mark can remove it")
    db.delete(row)
    db.commit()
    await ws_manager.broadcast({"type": "CASE_ANNOTATIONS_CHANGED", "caseId": case.id, "action": "deleted", "annotationId": annotation_id})
    return {"ok": True}
