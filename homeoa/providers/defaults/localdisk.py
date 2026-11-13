"""Default FileStorage: files on local disk under ``HOMEOA_FILE_ROOT``.

``HOMEOA_FILE_ROOT`` defaults to ``<BASE_DIR>/media/files`` (gitignored). Signed URLs point
at ``/files/<token>/`` (``homeoa.providers.views.download``); the token is signed with the
Django secret key and carries its expiry, so links stop working after ``expires_in``
(at most 5 minutes). See
``homeoa.providers.storage`` for the rules on using signed URLs.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.core import signing
from django.urls import reverse

from ..storage import MAX_SIGNED_URL_SECONDS, FileNotFound, FileStorage, StoredFile

SALT = "homeoa.providers.files"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_KEY = re.compile(r"^\d+/(?:[A-Za-z0-9_-]+/)*[0-9a-f]{32}-[A-Za-z0-9._-]{1,120}$")


def file_root() -> Path:
    root = getattr(settings, "HOMEOA_FILE_ROOT", None)
    return Path(root) if root else Path(settings.BASE_DIR) / "media" / "files"


def _safe(part: str, fallback: str) -> str:
    cleaned = _SAFE.sub("-", part).strip(".-")[:120]
    return cleaned or fallback


class LocalDiskStorage(FileStorage):
    def __init__(self, root: Path | str | None = None):
        self._root = Path(root) if root else None

    @property
    def root(self) -> Path:
        return self._root or file_root()

    def _path(self, key: str) -> Path:
        if not _KEY.match(key) or ".." in key:
            raise FileNotFound(key)
        return self.root / key

    def put(
        self,
        association_id: int,
        name: str,
        content: bytes | BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        folder: str = "",
    ) -> StoredFile:
        data = content if isinstance(content, bytes) else content.read()
        parts = [str(int(association_id))]
        parts += [_safe(p, "x").replace(".", "-") for p in folder.split("/") if p]
        key = "/".join([*parts, f"{uuid.uuid4().hex}-{_safe(name, 'file')}"])
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredFile(
            key=key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFound(key) from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except FileNotFound:
            return False

    def signed_url(
        self, key: str, *, association_id: int, expires_in: int = 300, filename: str = ""
    ) -> str:
        self._path(key)  # validates
        if not _belongs(key, association_id):
            raise FileNotFound(key)
        ttl = max(1, min(int(expires_in), MAX_SIGNED_URL_SECONDS))
        token = signing.dumps(
            {"k": key, "a": int(association_id), "f": filename, "x": int(time.time()) + ttl},
            salt=SALT,
        )
        return reverse("providers:download", args=[token])


def _belongs(key: str, association_id) -> bool:
    try:
        return key.split("/", 1)[0] == str(int(association_id))
    except TypeError, ValueError:
        return False


def resolve_token(token: str) -> tuple[str, str]:
    """``(key, filename)`` for a valid, unexpired token. Raises ``FileNotFound``."""
    try:
        data = signing.loads(token, salt=SALT)
    except signing.BadSignature as exc:
        raise FileNotFound("bad token") from exc
    if not isinstance(data, dict) or int(data.get("x", 0)) < time.time():
        raise FileNotFound("expired")
    if not _belongs(str(data.get("k", "")), data.get("a")):
        raise FileNotFound("association mismatch")
    return data["k"], data.get("f") or ""
