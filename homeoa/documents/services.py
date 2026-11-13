"""Document library (DOC-01) and governing-document versions (GOV-01).

``attach`` is the one entry point other apps (minutes, e-sign, compliance evidence) use to
file a document. ``can_view`` is the one visibility check: the board list, the resident
list and every download go through it. Nothing here deletes a file or a version.
"""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, F, Max, OuterRef, Q
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.providers import get_provider

from .models import (
    Category,
    DocStatus,
    Document,
    DocumentPage,
    DocumentVersion,
    GoverningKind,
    Visibility,
)

# Extension -> content type. Anything else is refused.
ALLOWED_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# Visibility -> the permission code that reveals it (None: anyone, signed in or not).
_VISIBILITY_PERM = {
    Visibility.PUBLIC: None,
    Visibility.TENANTS: perms.DOCUMENTS_TENANT,
    Visibility.OWNERS: perms.DOCUMENTS_READ,
    Visibility.BOARD: perms.DOCUMENTS_BOARD,
}


class UploadRejected(ValidationError):
    pass


def max_bytes() -> int:
    return int(getattr(settings, "HOMEOA_DOCUMENT_MAX_BYTES", DEFAULT_MAX_BYTES))


def can_manage(user, association) -> bool:
    return has_perm(user, perms.DOCUMENTS_MANAGE, association=association)


def can_view(user, association, visibility: str) -> bool:
    """May ``user`` list and download the current version of a document so visible?"""
    code = _VISIBILITY_PERM.get(visibility, perms.DOCUMENTS_BOARD)
    return code is None or has_perm(user, code, association=association)


def visible_levels(user, association) -> list[str]:
    return [v for v in Visibility.values if can_view(user, association, v)]


def can_download(user, association, version: DocumentVersion) -> bool:
    """Current version: whoever may view the document. Older versions: the board only."""
    doc = version.document
    if doc.association_id != association.pk:
        return False
    if version.pk != doc.current_version_id:
        return can_manage(user, association)
    return can_view(user, association, doc.visibility)


def text_query(query: str) -> SearchQuery:
    """``query`` as a web-style text search: words (any form: "assessments" finds
    "assessment"), "quoted phrases" and -excluded words."""
    from .text import SEARCH_CONFIG

    return SearchQuery(query, config=SEARCH_CONFIG, search_type="websearch")


def visible_documents(user, association, *, category: str = "", query: str = ""):
    """Documents the user may see; with ``query``, those whose title or file name contains
    it or whose current version's text matches it (DOC-05)."""
    qs = Document.all_associations.filter(
        association=association,
        visibility__in=visible_levels(user, association),
        current_version__isnull=False,
    ).select_related("current_version")
    if category:
        qs = qs.filter(category=category)
    if query:
        in_text = DocumentPage.all_associations.filter(
            version=OuterRef("current_version"), search=text_query(query)
        )
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(current_version__file_name__icontains=query)
            | Exists(in_text)
        )
    return qs


# Markers around matched words in a snippet; escaped text never contains them.
_MARK_START, _MARK_STOP = "\x01", "\x02"


def text_snippets(documents, query: str) -> dict[int, tuple[int, str]]:
    """``{document id: (page number, snippet HTML)}``: the best-matching page of each
    document's current version, with the matched words in ``<mark>``."""
    from .text import SEARCH_CONFIG

    versions = {d.current_version_id: d.pk for d in documents if d.current_version_id}
    if not query or not versions:
        return {}
    q = text_query(query)
    pages = (
        DocumentPage.all_associations.filter(version_id__in=versions, search=q)
        .annotate(
            rank=SearchRank(F("search"), q),
            snippet=SearchHeadline(
                "text",
                q,
                config=SEARCH_CONFIG,
                start_sel=_MARK_START,
                stop_sel=_MARK_STOP,
                max_words=30,
                min_words=12,
                max_fragments=1,
            ),
        )
        .order_by("version_id", "-rank", "number")
    )
    out: dict[int, tuple[int, str]] = {}
    for page in pages:
        doc_id = versions[page.version_id]
        if doc_id in out:
            continue
        html = escape(" ".join(page.snippet.split()))
        html = html.replace(_MARK_START, "<mark>").replace(_MARK_STOP, "</mark>")
        out[doc_id] = (page.number, mark_safe(html))  # noqa: S308 - escaped above
    return out


def check_upload(filename: str, size: int) -> str:
    """The content type for an acceptable upload; raises ``UploadRejected`` otherwise."""
    ext = PurePosixPath(filename or "").suffix.lower()
    if ext not in ALLOWED_TYPES:
        raise UploadRejected(
            _("%(name)s: this file type is not accepted (PDF, image, text, CSV, Word or Excel).")
            % {"name": filename}
        )
    if size <= 0:
        raise UploadRejected(_("%(name)s is empty.") % {"name": filename})
    if size > max_bytes():
        raise UploadRejected(
            _("%(name)s is larger than %(mb)s MB.")
            % {"name": filename, "mb": max_bytes() // (1024 * 1024)}
        )
    return ALLOWED_TYPES[ext]


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})


def check_photo(filename: str, size: int) -> None:
    """Accept only a PNG or JPEG within the upload limits; raises ``UploadRejected``."""
    if PurePosixPath(filename or "").suffix.lower() not in IMAGE_EXTENSIONS:
        raise UploadRejected(
            _("%(name)s: only photos (PNG or JPEG) can be attached.") % {"name": filename}
        )
    check_upload(filename, size)  # empty / size limit from storage settings


def _validate_choices(category, visibility, governing_kind):
    if category not in Category.values:
        raise UploadRejected(_("Unknown category."))
    if visibility not in Visibility.values:
        raise UploadRejected(_("Unknown visibility."))
    if governing_kind and governing_kind not in GoverningKind.values:
        raise UploadRejected(_("Unknown governing document kind."))
    if governing_kind and category != Category.GOVERNING:
        raise UploadRejected(_("Only governing documents have a governing kind."))


@transaction.atomic
def attach(
    association,
    *,
    filename: str,
    content: bytes,
    title: str = "",
    category: str = Category.OTHER,
    visibility: str = Visibility.BOARD,
    governing_kind: str = "",
    effective_date: dt.date | None = None,
    note: str = "",
    user=None,
    document: Document | None = None,
) -> DocumentVersion:
    """File ``content`` in the library and return the new version.

    With ``document`` the file becomes that document's new current version (category and
    visibility stay as they are); without it a new document is created. Older versions
    are kept and stay downloadable by the board.
    """
    ctype = check_upload(filename, len(content))
    if document is None:
        _validate_choices(category, visibility, governing_kind)
        document = Document.all_associations.create(
            association=association,
            title=(title or PurePosixPath(filename).stem)[:200],
            category=category,
            visibility=visibility,
            governing_kind=governing_kind,
        )
    elif document.association_id != association.pk:
        raise UploadRejected(_("That document belongs to another association."))
    else:
        document = Document.all_associations.select_for_update().get(pk=document.pk)
    stored = get_provider("FileStorage", association).put(
        association.pk, filename, content, content_type=ctype, folder="documents"
    )
    last = document.versions.aggregate(n=Max("number"))["n"] or 0
    version = DocumentVersion.all_associations.create(
        association=association,
        document=document,
        number=last + 1,
        file_key=stored.key,
        file_name=PurePosixPath(filename).name[:200],
        content_type=ctype,
        size=stored.size,
        sha256=stored.sha256,
        effective_date=effective_date,
        note=note[:500],
        uploaded_by=user if getattr(user, "is_authenticated", False) else None,
    )
    document.current_version = version
    document.save(update_fields=["current_version", "updated_at"])
    audit.record(
        "documents.uploaded",
        document,
        actor=user,
        changes={
            "version": version.number,
            "file_name": version.file_name,
            "sha256": version.sha256,
            "visibility": document.visibility,
        },
    )
    from .tasks import queue_read_text

    queue_read_text(version.pk)  # the text inside the file, for search (DOC-05)
    return version


def set_status(document: Document, status: str, *, note: str = "", link: str = "", user=None):
    """Mark a filed document rescinded or superseded (or back in force). Never deletes."""
    if status not in DocStatus.values:
        raise ValidationError(_("Unknown document status."))
    before = {"status": document.status, "status_note": document.status_note}
    document.status = status
    document.status_note = (note or "")[:300]
    document.status_link = (link or "")[:300]
    document.save(update_fields=["status", "status_note", "status_link", "updated_at"])
    audit.record(
        "documents.status_changed",
        document,
        actor=user,
        changes={"before": before, "status": status, "status_note": document.status_note},
    )
    return document


def set_visibility(document: Document, visibility: str, *, user=None) -> bool:
    if visibility not in Visibility.values:
        raise UploadRejected(_("Unknown visibility."))
    old = document.visibility
    if old == visibility:
        return False
    document.visibility = visibility
    if visibility == Visibility.BOARD:
        document.in_welcome_packet = False  # owners can't open it any more
    document.save(update_fields=["visibility", "in_welcome_packet", "updated_at"])
    audit.record(
        "documents.visibility_changed",
        document,
        actor=user,
        changes={"visibility": [old, visibility]},
    )
    return True


def read_file(version: DocumentVersion) -> bytes:
    return get_provider("FileStorage", version.document.association).get(version.file_key)
