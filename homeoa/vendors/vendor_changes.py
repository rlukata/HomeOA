"""Vendor changes that need board approval: adding a vendor, changing how it is
paid or its tax details (W-9), and deactivating it.

Changed payment details are the classic fraud path, so they never take effect on one
person's say-so. Name, category, expense account and notes stay direct edits for board
members. Secrets (TIN, account number) travel in the request encrypted, and a W-9 file is
stored when proposed (stored files are never deleted) so the request only holds its key.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.changes import kinds
from homeoa.changes import services as changes
from homeoa.core import crypto

from . import services
from .models import Vendor, mask_account, mask_tin

ADD, EDIT = "vendors.add", "vendors.edit"

# Fields that decide who gets paid, how, and what is reported to the IRS.
SENSITIVE = (
    "status",
    "legal_name",
    "tax_classification",
    "tin_type",
    "w9_received",
    "is_1099_eligible",
    "payee_name",
    "remit_address_line1",
    "remit_address_line2",
    "remit_city",
    "remit_region",
    "remit_postal_code",
)
PLAIN = ("name", "category", "default_expense_account", "notes")


def _to_json(value):
    if isinstance(value, dt.date):
        return value.isoformat()
    if hasattr(value, "pk"):
        return value.pk
    return value


def _field(name):
    return Vendor._meta.get_field(name)


def _from_json(name, value):
    field = _field(name)
    if value in (None, ""):
        return None if field.null else value
    if field.get_internal_type() == "DateField":
        return dt.date.fromisoformat(value)
    return value


def _show(vendor, name, value) -> str:
    field = _field(name)
    if value in (None, ""):
        return "—"
    if field.choices:
        return str(dict(field.flatchoices).get(value, value))
    if field.is_relation:
        obj = field.related_model.all_associations.filter(pk=value).first()
        return str(obj) if obj else str(value)
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    return str(value)


def _attname(name) -> str:
    return _field(name).attname


def _cleaned(form) -> dict:
    return {name: _to_json(form.cleaned_data.get(name)) for name in (*PLAIN, *SENSITIVE)}


def _secrets(form) -> dict:
    out = {}
    if form.cleaned_data.get("tin_input"):
        out["tin"] = crypto.encrypt(form.cleaned_data["tin_input"])
    account = (form.cleaned_data.get("account_input") or "").strip()
    if account:
        out["account_number"] = crypto.encrypt(account)
    return out


def _store_w9(association, upload) -> str:
    if not upload:
        return ""
    from homeoa.providers import get_provider

    stored = get_provider("FileStorage", association).put(
        association.pk,
        getattr(upload, "name", "") or "w9.pdf",
        upload,
        content_type=getattr(upload, "content_type", "") or "application/pdf",
        folder=services.W9_FOLDER,
    )
    return stored.key


def _secret_details(secrets: dict, w9_key: str, tin_type: str) -> list:
    rows = []
    if "tin" in secrets:
        rows.append((_("Taxpayer ID number"), mask_tin(crypto.decrypt(secrets["tin"]), tin_type)))
    if "account_number" in secrets:
        rows.append((_("Account number"), mask_account(crypto.decrypt(secrets["account_number"]))))
    if w9_key:
        rows.append((_("W-9"), _("New file attached")))
    return rows


def needs_approval(form, vendor: Vendor) -> bool:
    """Would saving this edit change payment, tax or active status?"""
    if _secrets(form) or form.cleaned_data.get("w9_file"):
        return True
    data = _cleaned(form)
    return any(data[name] != _to_json(getattr(vendor, name)) for name in SENSITIVE)


def propose_add(association, form, *, user):
    data = _cleaned(form)
    secrets = _secrets(form)
    w9_key = _store_w9(association, form.cleaned_data.get("w9_file"))
    details = [
        (str(_field(n).verbose_name).capitalize(), _show(None, n, data[n]))
        for n in (*PLAIN, *SENSITIVE)
        if data[n] not in (None, "", False)
    ] + _secret_details(secrets, w9_key, data["tin_type"])
    return changes.propose(
        association,
        ADD,
        {"fields": data, "secrets": secrets, "w9_key": w9_key},
        user=user,
        summary=_("Add vendor %(name)s") % {"name": data["name"]},
        details=details,
    )


def propose_edit(association, vendor: Vendor, form, *, user):
    data = _cleaned(form)
    changed = {n: v for n, v in data.items() if v != _to_json(getattr(vendor, n))}
    secrets = _secrets(form)
    w9_key = _store_w9(association, form.cleaned_data.get("w9_file"))
    details = [
        (
            str(_field(n).verbose_name).capitalize(),
            f"{_show(vendor, n, _to_json(getattr(vendor, n)))} → {_show(vendor, n, v)}",
        )
        for n, v in changed.items()
    ] + _secret_details(secrets, w9_key, data["tin_type"])
    if changed.get("status") == Vendor.Status.INACTIVE:
        summary = _("Deactivate vendor %(name)s") % {"name": vendor.name}
    else:
        summary = _("Change payment or tax details for %(name)s") % {"name": vendor.name}
    return changes.propose(
        association,
        EDIT,
        {"vendor": vendor.pk, "fields": changed, "secrets": secrets, "w9_key": w9_key},
        user=user,
        summary=summary,
        details=details,
    )


def _set(vendor: Vendor, payload: dict) -> None:
    for name, value in payload.get("fields", {}).items():
        setattr(vendor, _attname(name), _from_json(name, value))
    for name, token in payload.get("secrets", {}).items():
        setattr(vendor, name, crypto.decrypt(token))
    if payload.get("w9_key"):
        vendor.w9_file_key = payload["w9_key"]


@transaction.atomic
def _apply_add(change) -> str:
    vendor = Vendor(association=change.association)
    _set(vendor, change.payload)
    vendor.full_clean(exclude=["slug", "association"])
    services.save_vendor(vendor)
    return reverse("vendors:detail", args=[vendor.slug])


@transaction.atomic
def _apply_edit(change) -> str:
    vendor = Vendor.all_associations.select_for_update().get(
        association=change.association, pk=change.payload["vendor"]
    )
    _set(vendor, change.payload)
    vendor.full_clean(exclude=["slug", "association"])
    services.save_vendor(vendor)
    return reverse("vendors:detail", args=[vendor.slug])


def register() -> None:
    kinds.register(kinds.Kind(ADD, gettext_lazy("New vendor"), _apply_add))
    kinds.register(kinds.Kind(EDIT, gettext_lazy("Vendor payment details"), _apply_edit))
