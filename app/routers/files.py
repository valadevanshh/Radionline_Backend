import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

try:
    from app.config import settings
    from app.models import UserDB
    from app.security import get_current_user
    from app import storage
except ImportError:
    from app.config import settings
    from app.models import UserDB
    from app.security import get_current_user
    from app import storage

router = APIRouter(tags=["Files"])

ALLOWED_CATEGORIES = {"cases", "doctors", "centers"}


@router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Form("cases"),
    entity_id: str = Form(...),
    subfolder: str = Form("uploads"),
    _current_user: UserDB = Depends(get_current_user),
):
    if category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of: {', '.join(sorted(ALLOWED_CATEGORIES))}")
    if not entity_id.strip():
        raise HTTPException(status_code=400, detail="entity_id is required")

    rel = storage.save_upload_stream(
        file.file,
        category=category,
        entity_id=entity_id.strip(),
        subfolder=subfolder.strip() or "uploads",
        original_filename=file.filename or "upload.bin",
        content_type=file.content_type,
    )
    return {
        "path": rel,
        "url": storage.public_url(rel),
    }


@router.get("/files/{file_path:path}")
async def get_stored_file(file_path: str):
    """
    Serve a file from dated storage. Paths must stay under FILE_STORAGE_ROOT.
    For production on Hostinger, put nginx in front or restrict by network/VPN.
    """
    try:
        if storage.uses_sftp():
            data = storage.read_stored_bytes(file_path)
            media_type, _ = mimetypes.guess_type(file_path)
            return Response(content=data, media_type=media_type or "application/octet-stream")
        abs_path: Path = storage.absolute_path(file_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type, _ = mimetypes.guess_type(str(abs_path))
    return FileResponse(abs_path, media_type=media_type or "application/octet-stream")
