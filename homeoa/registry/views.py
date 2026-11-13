"""Units hub pages: All units, unit page, Owners & tenants.

Board pages: reads need ``units.read``, writes ``units.manage``. Residents get 403 here;
their unit views live under /me/.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.replay import change_view
from homeoa.core.context import get_current_association
from homeoa.providers import get_provider
from homeoa.reports.export import DATE, MONEY, NUMBER, Column, TableSpec
from homeoa.ui.export import export_format, export_response
from homeoa.ui.nav import get_hub

from . import export as records_export
from . import services, unit_sections
from .forms import (
    EndOccupancyForm,
    InterestsForm,
    NoticePreferenceForm,
    OccupancyForm,
    PersonForm,
    RecordsFullExportForm,
    TransferForm,
    UnitForm,
)
from .models import NoticePreference, Occupancy, OwnershipPeriod, Person, Unit


def _association():
    association = get_current_association()
    if association is None:
        raise Http404
    return association


def _render(request, template, context, tab=None):
    hub = get_hub("units")
    slugs = [t.slug for t in hub.subtabs]
    index = slugs.index(tab) if tab in slugs else -1
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": i == index}
        for i, t in enumerate(hub.subtabs)
    ]
    return render(request, template, {"current_hub": hub, "subtabs": tabs, **context})


def _sorted_units():
    return sorted(Unit.objects.all(), key=lambda u: u.sort_key)


def _form_errors(form, exc: ValidationError):
    for message in exc.messages:
        form.add_error(None, message)


def _can_see_money(request) -> bool:
    return has_perm(request.user, perms.FINANCE_READ, association=request.association)


def _balances(association, units, on):
    """``{unit_id: (balance, past_due)}`` from the ledger and open past-due charges."""
    from django.db.models import Sum

    from homeoa.billing.models import Charge
    from homeoa.ledger.services import unit_balance

    if association is None:
        return {}
    past_due = dict(
        Charge.all_associations.filter(association=association, open_balance__gt=0, due_date__lt=on)
        .values_list("unit_id")
        .annotate(s=Sum("open_balance"))
    )
    return {u.pk: (unit_balance(u.pk, association=association), past_due.get(u.pk)) for u in units}


@require_perm(perms.UNITS_READ)
def unit_list(request):
    association = get_current_association()  # None: empty list
    on = services.today_for(association)
    show_money = _can_see_money(request)
    units = _sorted_units()
    balances = _balances(association, units, on) if show_money else {}
    rows = []
    for unit in units:
        tenants = services.current_occupants(unit, on, kind=Occupancy.Kind.TENANT)
        balance, past_due = balances.get(unit.pk, (None, None))
        rows.append(
            {
                "unit": unit,
                "owners": services.current_owners(unit, on),
                "tenants": tenants,
                "balance": balance,
                "past_due": past_due,
                "balance_css": "num strong" if balance and balance > 0 else "num",
            }
        )
    if fmt := export_format(request):
        headers = [
            Column(_("Unit")),
            Column(_("Building")),
            Column(_("Owner")),
            Column(_("Tenant")),
            Column(_("Allocated interest %"), NUMBER),
        ]
        if show_money:
            headers += [Column(_("Balance"), MONEY), Column(_("Past due"), MONEY)]
        out = []
        for r in rows:
            line = [
                r["unit"].number,
                r["unit"].building,
                "; ".join(o.full_name for o in r["owners"]),
                "; ".join(t.person.full_name for t in r["tenants"]),
                r["unit"].allocated_interest,
            ]
            if show_money:
                line += [r["balance"], r["past_due"] or ""]
            out.append(line)
        spec = TableSpec(_("Units"), headers, out, filename="units", pii=True)
        return export_response(request, spec, fmt)
    total = services.interest_total(association)
    return _render(
        request,
        "registry/unit_list.html",
        {
            "rows": rows,
            "total": total,
            "total_ok": total == services.HUNDRED,
            "rented": sum(1 for r in rows if r["tenants"]),
            "show_money": show_money,
        },
        tab="all",
    )


@require_perm(perms.UNITS_MANAGE)
def unit_add(request):
    association = _association()
    form = UnitForm(request.POST or None, instance=Unit(association=association))
    if request.method == "POST" and form.is_valid():
        unit = form.save()
        messages.success(request, _("Unit added."))
        return redirect("registry:unit", number=unit.number)
    return _render(request, "registry/form.html", {"form": form, "title": _("Add unit")}, tab="all")


@require_perm(perms.UNITS_MANAGE)
def unit_edit(request, number):
    unit = get_object_or_404(Unit.objects, number=number)
    form = UnitForm(request.POST or None, instance=unit)
    if request.method == "POST" and form.is_valid():
        unit = form.save()
        messages.success(request, _("Unit saved."))
        if not services.interest_total_ok(unit.association):
            messages.warning(request, _("Allocated interests no longer add up to 100%."))
        return redirect("registry:unit", number=unit.number)
    return _render(
        request,
        "registry/form.html",
        {"form": form, "title": _("Edit %(unit)s") % {"unit": unit}, "back": unit},
        tab="all",
    )


@require_perm(perms.UNITS_MANAGE)
def interests(request):
    association = _association()
    units = _sorted_units()
    form = InterestsForm(request.POST or None, units=units)
    total = None
    if request.method == "POST" and form.is_valid():
        total = form.total()
        if total == services.HUNDRED or form.cleaned_data["save_anyway"]:
            with transaction.atomic():
                for unit in units:
                    value = form.cleaned_data[f"unit_{unit.pk}"]
                    if unit.allocated_interest != value:
                        unit.allocated_interest = value
                        unit.save(update_fields=["allocated_interest"])
            if total == services.HUNDRED:
                messages.success(request, _("Allocated interests saved."))
            else:
                messages.warning(
                    request,
                    _("Saved, but allocated interests add up to %(total)s%%, not 100%%.")
                    % {"total": f"{total:.4f}"},
                )
            return redirect("registry:index")
    current = services.interest_total(association)
    return _render(
        request,
        "registry/interests.html",
        {"form": form, "posted_total": total, "current_total": current},
        tab="all",
    )


def _unit_context(unit, transfer_form=None, occupancy_form=None):
    association = unit.association
    on = services.today_for(association)
    period = services.ownership_period_on(unit, on)
    periods = (
        OwnershipPeriod.objects.filter(unit=unit)
        .prefetch_related("shares__person")
        .order_by("-start")
    )
    occupants = Occupancy.objects.filter(unit=unit).select_related("person").order_by("-start")
    current_occ = [o for o in occupants if o.covers(on)]
    return {
        "unit": unit,
        "today": on,
        "period": period,
        "owners": [s.person for s in period.shares.all()] if period else [],
        "periods": periods,
        "occupants": occupants,
        "current_occupants": current_occ,
        "tenants": [o for o in current_occ if o.kind == Occupancy.Kind.TENANT],
        "vote_weight": services.vote_weight(unit),
        "vote_weighting": services.vote_weighting(association),
        "transfer_form": transfer_form,
        "occupancy_form": occupancy_form,
        "unit_sections": unit_sections.all_sections(),
    }


def _ledger_context(request, unit):
    from homeoa.billing.owner_ledger import owner_ledger

    if not _can_see_money(request):
        return {}
    on = services.today_for(unit.association)
    balance, past_due = _balances(unit.association, [unit], on)[unit.pk]
    rows = owner_ledger(unit)
    return {
        "show_money": True,
        "balance": balance,
        "past_due": past_due,
        "ledger_rows": list(reversed(rows)),
    }


@require_perm(perms.UNITS_READ)
def unit_detail(request, number):
    unit = get_object_or_404(Unit.objects, number=number)
    if fmt := export_format(request):
        if not _can_see_money(request):
            raise PermissionDenied
        from homeoa.billing.owner_ledger import owner_ledger

        spec = TableSpec(
            _("Unit %(n)s ledger") % {"n": unit.number},
            [
                Column(_("Date"), DATE),
                Column(_("Description")),
                Column(_("Charge"), MONEY),
                Column(_("Payment"), MONEY),
                Column(_("Balance"), MONEY),
            ],
            [
                (r.date, r.description, r.charge or "", r.payment or "", r.balance)
                for r in owner_ledger(unit)
            ],
            filename=f"unit-{unit.number}-ledger",
            pii=True,
        )
        return export_response(request, spec, fmt, audit_obj=unit)
    transfer_form = TransferForm() if request.GET.get("transfer") else None
    occupancy_form = OccupancyForm() if request.GET.get("occupant") else None
    context = _unit_context(unit, transfer_form, occupancy_form)
    context.update(_ledger_context(request, unit))
    return _render(request, "registry/unit_detail.html", context)


def _describe_transfer(request, number):
    p = request.POST
    owners = [p.get("new_owner_name", "")]
    if p.get("second_owner_name"):
        owners.append(p["second_owner_name"])
    current = (
        ", ".join(str(o) for o in services.current_owners(Unit.objects.get(number=number))) or "—"
    )
    details = [
        (_("Unit"), number),
        (_("Current owners"), current),
        (_("New owners"), ", ".join(o for o in owners if o)),
        (
            _("Emails"),
            ", ".join(e for e in (p.get("new_owner_email"), p.get("second_owner_email")) if e),
        ),
        (_("Effective"), p.get("effective_date", "")),
        (_("Notes"), p.get("notes", "")),
    ]
    summary = _("Transfer Unit %(unit)s to %(owners)s") % {
        "unit": number,
        "owners": " & ".join(o for o in owners if o),
    }
    return summary, [(k, v) for k, v in details if v]


# Ownership decides votes, balances and who gets statements: always a second board member.
@change_view(
    "registry.transfer",
    _l("Ownership transfer"),
    manage=perms.UNITS_MANAGE,
    propose=perms.UNITS_MANAGE,
    codes={perms.UNITS_MANAGE, perms.UNITS_READ},
    always=True,
    describe=_describe_transfer,
)
@require_perm(perms.UNITS_MANAGE)
def unit_transfer(request, number):
    unit = get_object_or_404(Unit.objects, number=number)
    form = TransferForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                owners = [
                    services.find_or_create_person(
                        unit.association, data["new_owner_name"], data["new_owner_email"]
                    )
                ]
                if data["second_owner_name"]:
                    owners.append(
                        services.find_or_create_person(
                            unit.association,
                            data["second_owner_name"],
                            data["second_owner_email"],
                        )
                    )
                if len({o.pk for o in owners}) != len(owners):
                    raise ValidationError(_("The same person is listed twice."))
                services.transfer_ownership(unit, owners, data["effective_date"], data["notes"])
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(request, _("Ownership transferred."))
            return redirect("registry:unit", number=unit.number)
    if request.headers.get("HX-Request") and request.method == "GET":
        return render(request, "registry/_transfer_form.html", {"unit": unit, "form": form})
    return _render(
        request,
        "registry/unit_detail.html",
        {**_unit_context(unit, transfer_form=form), **_ledger_context(request, unit)},
    )


@require_perm(perms.UNITS_MANAGE)
def occupant_add(request, number):
    unit = get_object_or_404(Unit.objects, number=number)
    form = OccupancyForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                person = services.find_or_create_person(
                    unit.association, data["name"], data["email"]
                )
                if data["phone"] and not person.phone:
                    person.phone = data["phone"]
                    person.save(update_fields=["phone"])
                services.add_occupancy(
                    unit,
                    person,
                    data["start"],
                    kind=data["kind"],
                    end=data["end"],
                    lease_key=services.store_lease(unit, data["lease_document"]),
                )
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(request, _("Occupant added."))
            return redirect("registry:unit", number=unit.number)
    if request.headers.get("HX-Request") and request.method == "GET":
        return render(request, "registry/_occupant_form.html", {"unit": unit, "form": form})
    return _render(
        request,
        "registry/unit_detail.html",
        {**_unit_context(unit, occupancy_form=form), **_ledger_context(request, unit)},
    )


@require_perm(perms.UNITS_READ)
def lease(request, number, pk):
    """Permission-checked lease download: redirect to a short-lived signed URL."""
    from homeoa.providers.storage import FileNotFound

    unit = get_object_or_404(Unit.objects, number=number)
    occ = get_object_or_404(Occupancy.objects, pk=pk, unit=unit)
    if not occ.lease_key:
        raise Http404
    try:
        url = services.lease_download_url(occ)
    except FileNotFound as exc:
        raise Http404 from exc
    response = redirect(url)
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "private, no-store"
    return response


@require_perm(perms.UNITS_MANAGE)
@require_POST
def occupant_end(request, number, pk):
    unit = get_object_or_404(Unit.objects, number=number)
    occ = get_object_or_404(Occupancy.objects, pk=pk, unit=unit)
    form = EndOccupancyForm(request.POST)
    if form.is_valid():
        occ.end = form.cleaned_data["end"]
        try:
            occ.full_clean()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            occ.save(update_fields=["end"])
            messages.success(request, _("Occupancy ended."))
    else:
        messages.error(request, _("Enter a valid end date."))
    return redirect("registry:unit", number=unit.number)


@require_perm(perms.UNITS_READ)
def people_list(request):
    association = get_current_association()  # None: empty list
    on = services.today_for(association)
    people = []
    for person in Person.objects.all():
        people.append(
            {
                "person": person,
                "owns": services.units_owned_by(person, on),
                "occupies": [
                    o.unit
                    for o in Occupancy.objects.filter(person=person).select_related("unit")
                    if o.covers(on)
                ],
                "pref": services.current_notice_preference(person),
            }
        )
    return _render(request, "registry/people_list.html", {"people": people}, tab="people")


@require_perm(perms.UNITS_MANAGE)
def person_add(request):
    association = _association()
    form = PersonForm(request.POST or None, instance=Person(association=association))
    if request.method == "POST" and form.is_valid():
        person = form.save()
        messages.success(request, _("Person added."))
        return redirect("registry:person", pk=person.pk)
    return _render(
        request, "registry/form.html", {"form": form, "title": _("Add person")}, tab="people"
    )


@require_perm(perms.UNITS_READ)
def person_detail(request, pk):
    person = get_object_or_404(Person.objects, pk=pk)
    on = services.today_for(person.association)
    shares = person.ownership_shares.select_related("period__unit").order_by("-period__start")
    return _render(
        request,
        "registry/person_detail.html",
        {
            "person": person,
            "today": on,
            "shares": shares,
            "occupancies": person.occupancies.select_related("unit").order_by("-start"),
            "preferences": person.notice_preferences.order_by("-recorded_at", "-pk"),
            "pref": services.current_notice_preference(person),
            "notice_form": NoticePreferenceForm(),
        },
        tab="people",
    )


@require_perm(perms.UNITS_MANAGE)
def person_edit(request, pk):
    person = get_object_or_404(Person.objects, pk=pk)
    form = PersonForm(request.POST or None, instance=person)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Person saved."))
        return redirect("registry:person", pk=person.pk)
    return _render(
        request,
        "registry/form.html",
        {"form": form, "title": _("Edit %(name)s") % {"name": person}, "back": person},
        tab="people",
    )


@require_perm(perms.UNITS_MANAGE)
@require_POST
def person_notices(request, pk):
    person = get_object_or_404(Person.objects, pk=pk)
    form = NoticePreferenceForm(request.POST, request.FILES)
    if form.is_valid():
        data = form.cleaned_data
        email = data["delivery"] == NoticePreference.Delivery.EMAIL
        try:
            if email and not person.email:
                raise ValidationError(_("Add an email address before choosing email notices."))
            evidence_key = evidence_name = ""
            if data.get("evidence"):
                upload = data["evidence"]
                stored = get_provider("FileStorage", person.association).put(
                    person.association_id,
                    f"consent-{person.pk}-{upload.name}",
                    upload.read(),
                    content_type=upload.content_type,
                    folder="registry/consents",
                )
                evidence_key, evidence_name = stored.key, upload.name[:200]
            services.record_notice_preference(
                person,
                data["delivery"],
                source=data["source"],
                note=data["note"],
                recorded_by=request.user,
                evidence_key=evidence_key,
                evidence_name=evidence_name,
                consent_text_version="paper" if email else "",
            )
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(request, _("Notice preference recorded."))
    else:
        for errors in form.errors.values():
            for message in errors:
                messages.error(request, message)
    return redirect("registry:person", pk=person.pk)


@require_perm(perms.UNITS_READ)
def person_consent_evidence(request, pk, pref_pk):
    """Download the scanned paper consent form."""
    from homeoa.providers.storage import FileNotFound

    pref = get_object_or_404(NoticePreference.objects, pk=pref_pk, person_id=pk)
    if not pref.evidence_key:
        raise Http404
    try:
        data = get_provider("FileStorage", pref.association).get(pref.evidence_key)
    except FileNotFound as exc:
        raise Http404 from exc
    audit.record("notice_preference.evidence_viewed", pref, actor=request.user)
    resp = HttpResponse(data, content_type="application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="{pref.evidence_name or "consent"}"'
    return resp


# ------------------------------------------------------------- owner records


def _can_export_records(request) -> bool:
    """``units.read`` plus the records permission (``units.manage``)."""
    user, assoc = request.user, request.association
    return has_perm(user, perms.UNITS_READ, association=assoc) and has_perm(
        user, perms.UNITS_MANAGE, association=assoc
    )


def _records_page(request, *, full_form=None, status=200):
    association = _association()
    can_full = has_perm(request.user, perms.UNITS_MANAGE, association=association)
    response = _render(
        request,
        "registry/records.html",
        {
            "can_full": can_full,
            "full_form": full_form or RecordsFullExportForm(),
        },
        tab="people",
    )
    response.status_code = status
    return response


@require_perm(perms.UNITS_READ)
def records(request):
    """Owner records export page; ``?format=`` downloads the redacted (owner) roster."""
    if not _can_export_records(request):
        raise PermissionDenied
    if fmt := export_format(request):
        spec = records_export.roster_spec(_association(), mode=records_export.OWNER)
        return export_response(request, spec, fmt)
    return _records_page(request)


@require_POST
@require_perm(perms.UNITS_MANAGE)
def records_full(request):
    """The full roster (no redaction): ``units.manage``, reason required, audited."""
    association = _association()
    if not _can_export_records(request):
        raise PermissionDenied
    form = RecordsFullExportForm(request.POST)
    if not form.is_valid():
        return _records_page(request, full_form=form, status=400)
    fmt = form.cleaned_data["format"]
    spec = records_export.roster_spec(association, mode=records_export.FULL)
    audit.record(
        "registry.records_full_export",
        association=association,
        actor=request.user,
        object_type="registry.records_export",
        object_repr=str(spec.title),
        changes={"format": fmt, "reason": form.cleaned_data["reason"], "rows": len(spec.rows)},
        message=form.cleaned_data["reason"],
    )
    return export_response(request, spec, fmt)
