"""A new bill filled in for someone to check.

Another part of HomeOA (the AI invoice reader) proposes a new bill's fields and its
invoice file. The proposal waits in the session and the file in storage; the usual Add
bill form opens filled in, and nothing is saved until the person saves or submits it.
Then the held file is attached to the bill and the audit log notes where the fields came
from. The file reference travels signed, so a form can only attach a file held here.
"""

from __future__ import annotations

import secrets

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa import audit

SESSION_KEY = "ap.prefill"
SALT = "ap.prefill.file"
HELD_FOLDER = "ap-held"
# A held file is attached within a day or not at all.
MAX_AGE_SECONDS = 24 * 60 * 60
# Proposals kept per session (the oldest are dropped).
KEEP = 5


def hold_file(association, name: str, content: bytes, content_type: str, source: str) -> str:
    """Store the invoice file until the bill is saved; return its signed reference."""
    from homeoa.providers import get_provider

    stored = get_provider("FileStorage", association).put(
        association.pk, name, content, content_type=content_type, folder=HELD_FOLDER
    )
    return signing.dumps(
        {"a": association.pk, "key": stored.key, "name": name, "type": content_type, "src": source},
        salt=SALT,
    )


def held_file(token: str, association) -> dict | None:
    """The held file a signed reference names, or ``None`` when it is not valid here."""
    try:
        data = signing.loads(token or "", salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    return data if data.get("a") == association.pk else None


def attach_held(bill, token: str, user) -> bool:
    """Attach the held file to ``bill``, note the proposal's source in the audit log and
    drop the held copy. ``False`` when the reference is not valid."""
    from homeoa.providers import get_provider

    from . import services

    data = held_file(token, bill.association)
    if data is None:
        return False
    storage = get_provider("FileStorage", bill.association)
    upload = SimpleUploadedFile(data["name"], storage.get(data["key"]), data["type"])
    services.add_attachment(bill, upload, user)
    storage.delete(data["key"])
    audit.record("ap.bill_prefilled", bill, actor=user, message=data["src"][:200])
    return True


def discard_held(token: str, association) -> None:
    """Drop a held file the person replaced with their own upload."""
    from homeoa.providers import get_provider

    data = held_file(token, association)
    if data is not None:
        get_provider("FileStorage", association).delete(data["key"])


def stash(request, proposal: dict) -> str:
    """Keep a proposal for the Add bill form; return the key for ``?prefill=``."""
    key = secrets.token_urlsafe(8)
    kept = dict(request.session.get(SESSION_KEY) or {})
    kept[key] = proposal
    request.session[SESSION_KEY] = dict(list(kept.items())[-KEEP:])
    return key


def take(request, key: str) -> dict | None:
    kept = dict(request.session.get(SESSION_KEY) or {})
    proposal = kept.pop(key or "", None)
    if proposal is not None:
        request.session[SESSION_KEY] = kept
    return proposal
