"""
Hostinger / KV2-friendly file storage.

Layout (under FILE_STORAGE_ROOT):
  YYYY/MM/DD/cases/{case_id}/uploads|clinical|snapshots|dicom/{filename}
  YYYY/MM/DD/doctors/{doctor_id}/signature|profile/{filename}
  YYYY/MM/DD/centers/{center_id}/logo|header/{filename}

Database stores the relative path (e.g. 2026/10/02/cases/abc/uploads/uuid.jpg).
API responses expose a public URL via public_url().
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Optional

try:
    from app.config import settings
except ImportError:
    from app.config import settings

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w/+\-.]+);base64,(?P<data>.+)$",
    re.DOTALL,
)
_STORED_PATH_RE = re.compile(r"^\d{4}/\d{2}/\d{2}/")


def uses_sftp() -> bool:
    return settings.FILE_STORAGE_TRANSPORT == "sftp"


def storage_root() -> Path:
    root = Path(settings.FILE_STORAGE_ROOT).resolve()
    if not uses_sftp():
        root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_relative(relative_path: str) -> str:
    rel = relative_path.replace("\\", "/").lstrip("/")
    if rel.startswith("api/files/"):
        rel = rel[len("api/files/") :]
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise ValueError("Invalid file path")
    return rel


def _sftp_session():
    import paramiko

    if not settings.FILE_SFTP_HOST or not settings.FILE_SFTP_PASSWORD:
        raise RuntimeError("FILE_SFTP_HOST and FILE_SFTP_PASSWORD are required for sftp storage")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        settings.FILE_SFTP_HOST,
        port=settings.FILE_SFTP_PORT,
        username=settings.FILE_SFTP_USER,
        password=settings.FILE_SFTP_PASSWORD,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return client, client.open_sftp()


def _sftp_makedirs(sftp, directory: str) -> None:
    current = ""
    for part in directory.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def _write_remote(rel: str, data: bytes) -> None:
    remote = f"{settings.FILE_SFTP_ROOT.rstrip('/')}/{rel}"
    client, sftp = _sftp_session()
    try:
        _sftp_makedirs(sftp, remote.rsplit("/", 1)[0])
        with sftp.file(remote, "wb") as handle:
            handle.write(data)
    finally:
        sftp.close()
        client.close()


def read_stored_bytes(relative_path: str) -> bytes:
    rel = _safe_relative(relative_path)
    if not uses_sftp():
        return absolute_path(rel).read_bytes()
    remote = f"{settings.FILE_SFTP_ROOT.rstrip('/')}/{rel}"
    client, sftp = _sftp_session()
    try:
        with sftp.file(remote, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise FileNotFoundError(rel) from exc
    finally:
        sftp.close()
        client.close()


def dated_prefix(when: datetime | None = None) -> str:
    dt = when or datetime.now(timezone.utc)
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"


def build_relative_path(
    *,
    category: str,
    entity_id: str,
    subfolder: str,
    filename: str,
    when: datetime | None = None,
) -> str:
    safe_entity = _sanitize_segment(entity_id)
    safe_sub = _sanitize_segment(subfolder)
    safe_name = _sanitize_filename(filename)
    return f"{dated_prefix(when)}/{category}/{safe_entity}/{safe_sub}/{safe_name}"


def _sanitize_segment(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "unknown").strip())
    return cleaned[:120] or "unknown"


def _sanitize_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", base.strip())
    return cleaned[:180] or f"{uuid.uuid4().hex}.bin"


def absolute_path(relative_path: str) -> Path:
    rel = _safe_relative(relative_path)
    root = storage_root()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError("Invalid file path")
    return target


def is_data_url(value: str | None) -> bool:
    return bool(value and value.startswith("data:") and ";base64," in value)


def is_http_url(value: str | None) -> bool:
    return bool(value and (value.startswith("http://") or value.startswith("https://")))


def is_stored_relative_path(value: str | None) -> bool:
    return bool(value and _STORED_PATH_RE.match(value.replace("\\", "/")))


def public_url(relative_or_absolute: str | None) -> str | None:
    if not relative_or_absolute:
        return relative_or_absolute
    raw = relative_or_absolute.strip()
    if is_data_url(raw) or is_http_url(raw):
        return raw
    if raw.startswith("/api/files/"):
        base = (settings.FILE_PUBLIC_BASE_URL or "").rstrip("/")
        if base:
            return f"{base}{raw}"
        return raw
    rel = raw.lstrip("/")
    if rel.startswith("api/files/"):
        rel = rel[len("api/files/") :]
    path_part = f"/api/files/{rel}"
    base = (settings.FILE_PUBLIC_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}{path_part}"
    return path_part


def public_url_list(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [public_url(v) or v for v in values]


def save_bytes(
    data: bytes,
    *,
    category: str,
    entity_id: str,
    subfolder: str,
    filename: str,
    content_type: str | None = None,
    when: datetime | None = None,
) -> str:
    rel = build_relative_path(
        category=category,
        entity_id=entity_id,
        subfolder=subfolder,
        filename=filename,
        when=when,
    )
    if uses_sftp():
        _write_remote(rel, data)
    else:
        dest = absolute_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    logger.info("Stored file %s (%s bytes)", rel, len(data))
    return rel


def save_data_url(
    data_url: str,
    *,
    category: str,
    entity_id: str,
    subfolder: str,
    filename_hint: str = "file",
) -> str:
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ValueError("Not a base64 data URL")
    mime = match.group("mime")
    b64 = match.group("data")
    raw = base64.b64decode(b64, validate=False)
    ext = mimetypes.guess_extension(mime) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    name = f"{uuid.uuid4().hex}{ext}" if not filename_hint else _sanitize_filename(filename_hint)
    if "." not in name:
        name = f"{name}{ext}"
    return save_bytes(
        raw,
        category=category,
        entity_id=entity_id,
        subfolder=subfolder,
        filename=name,
        content_type=mime,
    )


def materialize_media_reference(
    value: str | None,
    *,
    category: str,
    entity_id: str,
    subfolder: str,
    filename_hint: str = "file",
) -> str | None:
    if not value or not str(value).strip():
        return value
    raw = str(value).strip()
    if is_data_url(raw):
        return save_data_url(
            raw,
            category=category,
            entity_id=entity_id,
            subfolder=subfolder,
            filename_hint=filename_hint,
        )
    if is_stored_relative_path(raw):
        return raw.replace("\\", "/")
    if raw.startswith("/api/files/"):
        return raw[len("/api/files/") :]
    if is_http_url(raw):
        return raw
    return raw


def materialize_media_list(
    values: list[str] | None,
    *,
    category: str,
    entity_id: str,
    subfolder: str,
) -> list[str] | None:
    if not values:
        return values
    out: list[str] = []
    for idx, item in enumerate(values):
        if not item:
            continue
        stored = materialize_media_reference(
            item,
            category=category,
            entity_id=entity_id,
            subfolder=subfolder,
            filename_hint=f"{subfolder}-{idx + 1}",
        )
        if stored:
            out.append(stored)
    return out


def save_upload_stream(
    stream: BinaryIO,
    *,
    category: str,
    entity_id: str,
    subfolder: str,
    original_filename: str,
    content_type: str | None = None,
) -> str:
    ext = Path(original_filename).suffix or mimetypes.guess_extension(content_type or "") or ".bin"
    filename = f"{uuid.uuid4().hex}{ext}"
    rel = build_relative_path(
        category=category,
        entity_id=entity_id,
        subfolder=subfolder,
        filename=filename,
    )
    data = stream.read()
    if uses_sftp():
        _write_remote(rel, data)
    else:
        dest = absolute_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return rel
