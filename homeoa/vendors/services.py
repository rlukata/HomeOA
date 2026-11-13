"""Vendor directory services: create, W-9 files, audited TIN reveal."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.utils.text import slugify

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.services import has_perm

from .models import Vendor, format_tin

W9_FOLDER = "vendors/w9"
# Words a vendor slug must not take: sub-routes and hub tabs under /vendors/.
RESERVED_SLUGS = frozenset(
    {"add", "certificates", "contracts", "alerts", "insurance", "1099", "all"}
)


def unique_slug(name: str, association, exclude_pk=None) -> str:
    base = slugify(name)[:70] or "vendor"
    if base in RESERVED_SLUGS:
        base = f"{base}-vendor"
    slug, n = base, 2
    qs = Vendor.all_associations.filter(association=association)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    while qs.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def save_vendor(vendor: Vendor) -> Vendor:
    """Save, giving a new vendor a slug unique in its association (audited via register)."""
    if not vendor.slug:
        from homeoa.core.context import get_current_association

        association = vendor.association if vendor.association_id else get_current_association()
        vendor.slug = unique_slug(vendor.name, association)
    vendor.save()
    return vendor


def search(qs, query: str = "", status: str = ""):
    if status in Vendor.Status.values:
        qs = qs.filter(status=status)
    query = (query or "").strip()
    if query:
        qs = qs.filter(
            Q(name__icontains=query)
            | Q(category__icontains=query)
            | Q(legal_name__icontains=query)
            | Q(contacts__name__icontains=query)
        ).distinct()
    return qs


def paid_by_vendor(association, today: dt.date) -> dict[int, Decimal]:
    """Total of paid bills per vendor id for the association year containing ``today``."""
    from homeoa.ap.models import Bill, BillLine
    from homeoa.closing.services import fiscal_year_bounds
    from homeoa.reports.statements import fiscal_year_start

    start, end = fiscal_year_bounds(association, fiscal_year_start(association, today).year)
    rows = (
        BillLine.all_associations.filter(
            association=association,
            bill__status=Bill.Status.PAID,
            bill__paid_on__gte=start,
            bill__paid_on__lte=end,
        )
        .order_by()
        .values("bill__vendor_id")
        .annotate(total=Sum("amount"))
    )
    return {r["bill__vendor_id"]: r["total"] for r in rows}


def reveal_tin(vendor: Vendor, user) -> str:
    """The TIN in clear, for a user with ``finance.manage``. Every reveal is audited."""
    if not has_perm(user, perms.FINANCE_MANAGE, association=vendor.association):
        raise PermissionDenied
    audit.record("vendors.tin_revealed", vendor, actor=user)
    return format_tin(vendor.tin, vendor.tin_type)


def store_w9(vendor: Vendor, upload) -> str:
    from homeoa.providers import get_provider

    storage = get_provider("FileStorage", vendor.association)
    stored = storage.put(
        vendor.association_id,
        getattr(upload, "name", "") or "w9.pdf",
        upload,
        content_type=getattr(upload, "content_type", "") or "application/pdf",
        folder=W9_FOLDER,
    )
    vendor.w9_file_key = stored.key
    vendor.save(update_fields=["w9_file_key"])
    return stored.key


def store_file(obj, upload, folder: str) -> str:
    """Attach an uploaded file to a VendorDocument or Contract. Stored files are never
    deleted; a replacement upload is stored under a new key."""
    from homeoa.providers import get_provider

    storage = get_provider("FileStorage", obj.association)
    name = getattr(upload, "name", "") or "file.pdf"
    stored = storage.put(
        obj.association_id,
        name,
        upload,
        content_type=getattr(upload, "content_type", "") or "application/octet-stream",
        folder=folder,
    )
    obj.file_key, obj.file_name = stored.key, name[:200]
    obj.save(update_fields=["file_key", "file_name"])
    return stored.key


def file_content(obj) -> bytes:
    from homeoa.providers import get_provider

    return get_provider("FileStorage", obj.association).get(obj.file_key)


def w9_content(vendor: Vendor) -> bytes:
    from homeoa.providers import get_provider

    return get_provider("FileStorage", vendor.association).get(vendor.w9_file_key)
