"""FileStorage: association-scoped file storage (local disk by default, S3 etc. by plugin).

Keys are opaque strings returned by ``put``; they always start with the association id, so
one association can never address another's files.

Signed download URLs
--------------------
``signed_url`` links need no login, so they are only allowed under these rules:

* Only permission-checked app views mint them, and those views redirect straight to the
  URL (never render it into a page).
* URLs never go into emails, notices or cached pages. Mail links point at the app view.
* Expiry is at most ``MAX_SIGNED_URL_SECONDS`` (5 minutes); longer requests are capped.
* The download response sends ``Referrer-Policy: no-referrer``.
* The token is bound to its association: ``signed_url`` takes ``association_id``, refuses a
  key from another association, and the download checks the key prefix again.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import BinaryIO

MAX_SIGNED_URL_SECONDS = 300


class FileNotFound(LookupError):
    pass


@dataclasses.dataclass(frozen=True, kw_only=True)
class StoredFile:
    key: str
    size: int
    sha256: str
    content_type: str = "application/octet-stream"


class FileStorage(abc.ABC):
    @abc.abstractmethod
    def put(
        self,
        association_id: int,
        name: str,
        content: bytes | BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        folder: str = "",
    ) -> StoredFile: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes:
        """Raises ``FileNotFound``."""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Deleting a missing key is not an error."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def signed_url(
        self, key: str, *, association_id: int, expires_in: int = 300, filename: str = ""
    ) -> str:
        """A URL anyone holding it can download from until it expires.

        ``expires_in`` is capped at ``MAX_SIGNED_URL_SECONDS``. Raises ``FileNotFound`` if
        ``key`` is not in ``association_id``'s space. See the module docstring for the rules.
        """
