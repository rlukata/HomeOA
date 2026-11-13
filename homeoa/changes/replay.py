"""Screens whose changes need board approval.

``@change_view(key, label, manage=..., propose=...)`` goes on a view, above its permission
check. ``@money_change(key, label)`` is the money version (Treasurer manages, other board
members propose); ownership transfers and policy settings use ``always=True``.

For a money screen:

* The Treasurer (``finance.manage``) works directly, as before.
* Another board member (``finance.propose``) sees the same screen and submits the same
  form. The view runs once inside a transaction that is always rolled back: if it fails
  (form errors, a refused change) the person sees the errors as usual; if it would succeed,
  nothing is kept and a change request records the submitted form instead.
* With ``always=True`` (credits, write-offs, waived fees, reopening a closed period) even
  the Treasurer goes through a change request.

When the request is approved, the same view runs again for real, as the person who
proposed it (so "posted by" stays theirs), with money access for that one call. Uploaded
files are kept in file storage between the two runs. A run that no longer succeeds (the
charge was paid meanwhile, the period was closed) marks the request as not applied, with
the reason, and changes nothing.
"""

from __future__ import annotations

import datetime as dt
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from importlib import import_module

from django.conf import settings
from django.contrib import messages
from django.contrib.messages import constants as message_levels
from django.contrib.messages.storage.base import BaseStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import elevated, has_perm
from homeoa.audit.context import audit_context
from homeoa.core.context import association_context

from . import kinds
from . import services as changes
from .models import ChangeRequest

FILE_FOLDER = "changes"
# What a replayed money view may do: money changes, and reading the money pages it sits on.
MONEY_CODES = frozenset({perms.FINANCE_MANAGE, perms.FINANCE_READ})
_REPLAYING: ContextVar[bool] = ContextVar("homeoa_changes_replaying", default=False)
_SKIP_FIELDS = {"csrfmiddlewaretoken"}


class _Collect(BaseStorage):
    """Messages a view adds during a dry run or replay, kept in memory only."""

    def _get(self, *args, **kwargs):
        return [], True

    def _store(self, messages, response, *args, **kwargs):
        return []

    @property
    def collected(self):
        return list(self._queued_messages)


class _Rollback(Exception):
    pass


@dataclass
class _Outcome:
    response: object
    messages: list
    ok: bool


def _succeeded(response, collected, wrote: bool) -> bool:
    """The view changed something (every money record is audited) and reported no error."""
    if any(m.level >= message_levels.ERROR for m in collected):
        return False
    return wrote and response.status_code < 400


def _location(response) -> str:
    for header in ("Location", "HX-Redirect", "HX-Location"):
        if response.has_header(header):
            return response[header]
    return ""


def _run(view, request, args, kwargs, *, user, codes) -> _Outcome:
    from homeoa.audit.models import AuditEvent

    last = AuditEvent.all_associations.order_by("-pk").values_list("pk", flat=True).first() or 0
    real = getattr(request, "_messages", None)
    request._messages = _Collect(request)
    try:
        with elevated(user, codes):
            response = view(request, *args, **kwargs)
        collected = request._messages.collected
    finally:
        if real is not None:
            request._messages = real
    wrote = AuditEvent.all_associations.filter(pk__gt=last).exists()
    return _Outcome(response, collected, _succeeded(response, collected, wrote))


def _dry_run(view, request, args, kwargs, codes) -> _Outcome:
    outcome = None
    try:
        with transaction.atomic():
            outcome = _run(view, request, args, kwargs, user=request.user, codes=codes)
            raise _Rollback
    except _Rollback:
        pass
    # The rollback undid the database, not objects in memory: reload the association, whose
    # settings (rule overrides, limits) the view may have changed on this very instance.
    association = getattr(request, "association", None)
    if association is not None:
        association.refresh_from_db()
    for _field, uploads in request.FILES.lists():
        for upload in uploads:
            upload.seek(0)
    return outcome


def _post_data(request) -> dict:
    return {k: v for k, v in request.POST.lists() if k not in _SKIP_FIELDS}


def _store_files(request) -> dict:
    if not request.FILES:
        return {}
    from homeoa.providers import get_provider

    storage = get_provider("FileStorage", request.association)
    out = {}
    for field, uploads in request.FILES.lists():
        out[field] = []
        for upload in uploads:
            upload.seek(0)
            stored = storage.put(
                request.association.pk,
                upload.name,
                upload,
                content_type=upload.content_type or "application/octet-stream",
                folder=FILE_FOLDER,
            )
            out[field].append(
                {"key": stored.key, "name": upload.name, "type": upload.content_type or ""}
            )
    return out


def _default_details(request) -> list:
    rows = []
    for key, values in _post_data(request).items():
        shown = ", ".join(v for v in values if v)
        if shown:
            rows.append((key.replace("_", " ").capitalize(), shown))
    for field, uploads in request.FILES.lists():
        rows.append((field.replace("_", " ").capitalize(), ", ".join(u.name for u in uploads)))
    return rows


def _go(request, url):
    if getattr(request, "htmx", False):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _proposed_response(request, change, fallback: str):
    if change.status == ChangeRequest.Status.APPLIED:
        # Applied at once (nobody else could approve): show what the screen itself said.
        said = getattr(change, "replay_messages", None)
        for m in said or ():
            messages.add_message(request, m.level, m.message, extra_tags=m.extra_tags)
        if not said:
            messages.success(request, _("Saved."))
        return _go(request, change.result_url or fallback)
    if change.status == ChangeRequest.Status.FAILED:
        messages.error(request, change.error)
        return _go(request, fallback)
    if change.resolution_id:
        messages.success(
            request,
            _("Saved. It takes effect when resolution %(number)s is adopted.")
            % {"number": change.resolution.number},
        )
        return _go(request, reverse("changes:detail", args=[change.pk]))
    messages.success(
        request,
        _("Sent for board approval. It takes effect once approved (%(n)s needed).")
        % {"n": change.required_approvals},
    )
    return _go(request, reverse("changes:detail", args=[change.pk]))


def is_replaying() -> bool:
    return _REPLAYING.get()


def change_view(
    key: str,
    label,
    *,
    manage: str,
    propose: str,
    codes,
    always=False,
    describe=None,
    by_resolution=False,
):
    """Make a view propose a change request instead of acting (see module doc).

    ``manage`` acts directly (unless ``always``); ``propose`` may only propose. ``codes``
    are what the view needs while it runs for a proposer (dry run and approved replay).
    ``describe(request, *args, **kwargs) -> (summary, details)`` words the request for the
    approvers; without it the submitted fields are listed. With ``by_resolution`` the form
    carries a board resolution (``changes.forms.add_resolution_field``) that approves it.
    """
    kinds.register(kinds.Kind(key, label, apply_replay, by_resolution=by_resolution))
    codes = frozenset(codes)

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if _REPLAYING.get():
                return view(request, *args, **kwargs)
            user, association = request.user, getattr(request, "association", None)
            if not user.is_authenticated or association is None:
                return view(request, *args, **kwargs)
            can_manage = has_perm(user, manage, association=association)
            can_propose = has_perm(user, propose, association=association)
            if request.method != "POST":
                if can_propose and not can_manage:
                    with elevated(user, codes):
                        return view(request, *args, **kwargs)
                return view(request, *args, **kwargs)
            if (can_manage and not always) or not (can_manage or can_propose):
                return view(request, *args, **kwargs)
            outcome = _dry_run(view, request, args, kwargs, codes)
            if not outcome.ok:
                for m in outcome.messages:
                    messages.add_message(request, m.level, m.message, extra_tags=m.extra_tags)
                return outcome.response
            if describe is not None:
                summary, details = describe(request, *args, **kwargs)
            else:
                summary, details = str(label), _default_details(request)
            payload = {
                "path": request.path,
                "post": _post_data(request),
                "files": _store_files(request),
                "codes": sorted(codes),
            }
            resolution = None
            if by_resolution:
                from homeoa.governance.models import Resolution

                resolution = Resolution.all_associations.filter(
                    association=association, pk=request.POST.get("change_resolution") or 0
                ).first()
                details = [*details, (_("Board resolution"), str(resolution or ""))]
            change = changes.propose(
                association,
                key,
                payload,
                user=user,
                summary=summary,
                details=details,
                resolution=resolution,
            )
            return _proposed_response(
                request, change, _location(outcome.response) or request.get_full_path()
            )

        wrapped.change_kind = key
        return wrapped

    return decorator


def money_change(key: str, label, *, always: bool = False, describe=None, by_resolution=False):
    """A money screen: the Treasurer acts, other board members propose."""
    return change_view(
        key,
        label,
        manage=perms.FINANCE_MANAGE,
        propose=perms.FINANCE_PROPOSE,
        codes=MONEY_CODES,
        always=always or by_resolution,
        describe=describe,
        by_resolution=by_resolution,
    )


def money_page(view):
    """A read-only money page that leads to a proposal (e.g. the dues preview): board
    members who propose money changes may open it as the Treasurer can."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user, association = request.user, getattr(request, "association", None)
        if (
            user.is_authenticated
            and association is not None
            and has_perm(user, perms.FINANCE_PROPOSE, association=association)
            and not has_perm(user, perms.FINANCE_MANAGE, association=association)
        ):
            with elevated(user, MONEY_CODES):
                return view(request, *args, **kwargs)
        return view(request, *args, **kwargs)

    return wrapped


def _replay_request(change):
    payload = change.payload
    data = {k: v if len(v) != 1 else v[0] for k, v in payload.get("post", {}).items()}
    if payload.get("files"):
        from homeoa.providers import get_provider

        storage = get_provider("FileStorage", change.association)
        for field, uploads in payload["files"].items():
            files = [
                SimpleUploadedFile(u["name"], storage.get(u["key"]), content_type=u["type"])
                for u in uploads
            ]
            data[field] = files if len(files) != 1 else files[0]
    request = RequestFactory().post(payload["path"], data=data)
    request.user = change.proposed_by
    request.association = change.association
    request.htmx = False
    request.csp_nonce = ""
    request.session = import_module(settings.SESSION_ENGINE).SessionStore()
    return request


def apply_replay(change) -> str:
    """Run the proposed form again, for real, as its proposer."""
    request = _replay_request(change)
    match = resolve(change.payload["path"])
    token = _REPLAYING.set(True)
    try:
        with association_context(change.association), audit_context(actor=change.proposed_by):
            outcome = _run(
                match.func,
                request,
                match.args,
                match.kwargs,
                user=change.proposed_by,
                codes=change.payload.get("codes") or MONEY_CODES,
            )
    finally:
        _REPLAYING.reset(token)
    change.replay_messages = outcome.messages
    if not outcome.ok:
        errors = [str(m.message) for m in outcome.messages if m.level >= message_levels.WARNING]
        raise changes.ChangeError(
            "; ".join(errors)
            or _(
                "The change no longer goes through as proposed (it may have been made "
                "already, or something it depends on changed)."
            )
        )
    return _location(outcome.response)


def money_value(value) -> str:
    """``"$1,234.50"`` for an amount typed into a form (shown to approvers)."""
    from decimal import Decimal, InvalidOperation

    try:
        return f"${Decimal(str(value).replace(',', '')):,.2f}"
    except InvalidOperation, ValueError:
        return str(value)


def date_value(value) -> str:
    try:
        return dt.date.fromisoformat(value).strftime("%b %-d, %Y")
    except TypeError, ValueError:
        return str(value or "")
