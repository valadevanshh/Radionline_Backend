"""Validated multi-file profile attachments, stored alongside existing media metadata."""
import base64
import re
import uuid
from typing import Optional
from fastapi import HTTPException
from pydantic import BaseModel, Field
from app import storage

MAX_FILES = 20
MAX_BYTES = 5 * 1024 * 1024
MAX_BATCH_BYTES = 15 * 1024 * 1024
TYPES = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
         "application/msword": ".doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}

class ProfileDocument(BaseModel):
    id: Optional[str] = Field(default=None, max_length=80)
    url: str
    fileName: str = Field(max_length=150)

def public_documents(values):
    return [{"id": v["id"], "fileName": v["fileName"], "url": storage.public_url(v["url"])}
            for v in (values or []) if isinstance(v, dict) and v.get("id") and v.get("url") and v.get("fileName")]

def prepare_documents(values, existing=None):
    """Validate the entire selection before writing any files. Existing references must belong to this profile."""
    if len(values) > MAX_FILES:
        raise HTTPException(400, f"You can attach up to {MAX_FILES} documents")
    current = {v["id"]: v for v in (existing or []) if isinstance(v, dict) and v.get("id")}
    prepared, seen, total = [], set(), 0
    for item in values:
        value = item.model_dump() if isinstance(item, BaseModel) else item
        url = value.get("url", "")
        if url.startswith("data:"):
            match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)", url)
            if not match or match[1].lower() not in TYPES:
                raise HTTPException(400, "Documents must be PDF, DOC, DOCX, PNG, JPG or WEBP files")
            if len(match[2]) > (MAX_BYTES * 4 // 3) + 8:
                raise HTTPException(400, "Each document must be 5 MB or smaller")
            try:
                data = base64.b64decode(match[2], validate=True)
            except Exception:
                raise HTTPException(400, "Document could not be read")
            if not data or len(data) > MAX_BYTES:
                raise HTTPException(400, "Each document must be non-empty and 5 MB or smaller")
            total += len(data)
            if total > MAX_BATCH_BYTES:
                raise HTTPException(400, "New documents must total 15 MB or less per save")
            mime = match[1].lower()
            name = re.split(r"[\\/]", str(value.get("fileName", "")))[-1]
            name = re.sub(r'[\x00-\x1f<>:"|?*]', '', name).strip(' .')[:140] or 'Document'
            ext = TYPES[mime]
            if not name.lower().endswith(ext):
                name = re.sub(r"\.[a-zA-Z0-9]+$", "", name) + ext
            prepared.append({"id": uuid.uuid4().hex, "fileName": name, "data": data, "mime": mime, "ext": ext})
        else:
            previous = current.get(value.get("id"))
            if not previous or url not in (previous["url"], storage.public_url(previous["url"])):
                raise HTTPException(400, "Unknown document reference; please reload the profile")
            if previous["id"] in seen:
                raise HTTPException(400, "Duplicate document reference")
            seen.add(previous["id"])
            prepared.append(dict(previous))
    return prepared

def store_documents(prepared, category, entity_id):
    saved = []
    for item in prepared:
        if "data" not in item:
            saved.append(item)
            continue
        url = storage.save_bytes(item["data"], category=category, entity_id=entity_id, subfolder="documents",
                                 filename=f"{item['id']}{item['ext']}", content_type=item["mime"])
        saved.append({"id": item["id"], "fileName": item["fileName"], "url": url})
    return saved
