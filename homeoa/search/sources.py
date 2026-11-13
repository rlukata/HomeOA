"""Core search sources: units, people, vendors, bills, documents and meetings.

Optional modules (violations, design requests, maintenance requests, work orders)
register their own sources from their ready hooks.
"""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_any, has_perm
from homeoa.plugins import slots

from .services import LIMIT, Hit, name_match


def units(user, association, query):
    from homeoa.core.context import association_today
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    if not has_perm(user, perms.UNITS_READ, association=association):
        return []
    number = query[5:].strip() if query.lower().startswith("unit ") else query
    found = Unit.all_associations.filter(association=association).filter(
        Q(number__iexact=number)
        | Q(number__icontains=number)
        | Q(building__icontains=query)
        | Q(parcel_id__icontains=query)
    )
    today = association_today(association)
    out = []
    for unit in sorted(found, key=lambda u: (u.number.lower() != number.lower(), u.sort_key)):
        owners = ", ".join(p.full_name for p in current_owners(unit, today))
        out.append(Hit(_("Units"), str(unit), reverse("registry:unit", args=[unit.number]), owners))
    return out[:LIMIT]


def people(user, association, query):
    from homeoa.registry.models import Person

    if not has_perm(user, perms.UNITS_READ, association=association):
        return []
    found = (
        Person.all_associations.filter(association=association)
        .filter(
            name_match("full_name", query)
            | Q(preferred_name__icontains=query)
            | Q(email__icontains=query)
            | Q(alt_email__icontains=query)
            | Q(phone__icontains=query)
        )
        .order_by("full_name", "pk")[:LIMIT]
    )
    return [
        Hit(_("People"), p.full_name, reverse("registry:person", args=[p.pk]), p.email)
        for p in found
    ]


def vendors(user, association, query):
    from homeoa.vendors.models import Vendor

    if not has_any(user, (perms.VENDORS_READ, perms.VENDORS_MANAGE), association=association):
        return []
    found = (
        Vendor.all_associations.filter(association=association)
        .filter(
            name_match("name", query)
            | Q(legal_name__icontains=query)
            | Q(payee_name__icontains=query)
            | Q(category__icontains=query)
        )
        .order_by("status", "name")[:LIMIT]
    )
    out = []
    for v in found:
        detail = v.category
        if v.status == Vendor.Status.INACTIVE:
            detail = ", ".join(x for x in (detail, str(_("inactive"))) if x)
        out.append(Hit(_("Vendors"), v.name, reverse("vendors:detail", args=[v.slug]), detail))
    return out


def bills(user, association, query):
    from homeoa.ap.models import Bill
    from homeoa.ledger.templatetags.ledger_tags import money

    codes = (perms.FINANCE_READ, perms.FINANCE_MANAGE, perms.BILLS_ENTER)
    if not has_any(user, codes, association=association):
        return []
    found = (
        Bill.all_associations.filter(association=association)
        .filter(
            Q(invoice_number__icontains=query)
            | Q(description__icontains=query)
            | name_match("vendor__name", query)
        )
        .select_related("vendor")
        .prefetch_related("lines")
        .order_by("-bill_date", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Bills"),
            _("%(vendor)s, invoice %(number)s")
            % {"vendor": b.vendor.name, "number": b.invoice_number},
            reverse("ap:detail", args=[b.pk]),
            " · ".join(
                (date_format(b.bill_date, "M j, Y"), money(b.total), str(b.get_status_display()))
            ),
        )
        for b in found
    ]


def documents(user, association, query):
    from homeoa.documents import services as docs

    found = list(
        docs.visible_documents(user, association, query=query).order_by("-updated_at")[:LIMIT]
    )
    snippets = docs.text_snippets(found, query)
    out = []
    for d in found:
        page, snippet = snippets.get(d.pk, (None, ""))
        detail = str(d.get_category_display())
        if page is not None:
            detail = _("%(category)s, page %(page)s") % {"category": detail, "page": page}
        out.append(
            Hit(
                _("Documents"),
                d.title,
                reverse("documents:detail", args=[d.public_id]),
                detail,
                snippet,
            )
        )
    return out


def meetings(user, association, query):
    from homeoa.meetings.models import Meeting

    if not has_perm(user, perms.GOVERNANCE_READ, association=association):
        return []
    found = (
        Meeting.all_associations.filter(association=association)
        .filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(location__icontains=query)
        )
        .order_by("-date", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Meetings"),
            m.title or str(m.get_kind_display()),
            reverse("meetings:detail", args=[m.pk]),
            date_format(m.date, "M j, Y"),
        )
        for m in found
    ]


def register() -> None:
    for order, source in enumerate((units, people, vendors, bills, documents, meetings)):
        slots.register(slots.SEARCH_SOURCES, source, order=order * 10)
