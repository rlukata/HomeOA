"""Profile (POR-05, REG-04 UI): contact info, household, notification preferences, consent.

Every change goes through a registry or notify service, so it is audited (registry models
are registered with :mod:`homeoa.audit`; consent records are append-only).

E-notice consent: the owner confirms the address and accepts the versioned text in
:mod:`homeoa.registry.consent` (draft, attorney review pending). The same form is offered
on Home at first sign-in after an invitation (source "invite") and here (source "portal").
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa.access.decorators import resident_view
from homeoa.access.services import today_for
from homeoa.notify import channels as ch
from homeoa.notify import services as notify_services
from homeoa.notify import types as ntypes
from homeoa.plugins import slots
from homeoa.registry import consent as consent_text
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Occupancy, Person

from .base import _render
from .mine import my_person


def consent_context() -> dict:
    """Template context for ``portal/_consent_form.html``."""
    return {
        "consent_text": consent_text.disclosures(),
        "consent_version": consent_text.VERSION,
        "consent_review": consent_text.ATTORNEY_REVIEW,
    }


def needs_consent_prompt(person) -> bool:
    """Ask at first sign-in: the owner has not yet chosen in HomeOA."""
    if person is None:
        return False
    return not person.notice_preferences.filter(
        source__in=(
            NoticePreference.Source.PORTAL,
            NoticePreference.Source.INVITE,
            NoticePreference.Source.PAPER,
        )
    ).exists()


#: Channels a resident can pick for optional notifications.
PREF_CHANNELS = (
    (ch.INAPP, gettext_lazy("In the app")),
    (ch.EMAIL, gettext_lazy("Email")),
)


class ContactForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = list(reg.CONTACT_FIELDS)


class HouseholdForm(forms.Form):
    full_name = forms.CharField(label=gettext_lazy("Name"), max_length=200)
    email = forms.EmailField(label=gettext_lazy("Email (optional)"), required=False)


def _require_person(request) -> Person:
    person = my_person(request)
    if person is None:
        raise PermissionDenied
    return person


def _owned_units(person, today):
    return sorted(reg.units_owned_by(person, today), key=lambda u: u.sort_key)


def _household(units, today):
    return [
        o
        for o in Occupancy.objects.filter(
            unit__in=units, kind=Occupancy.Kind.RESIDENT
        ).select_related("person", "unit")
        if o.covers(today) or o.start > today
    ]


def _pref_rows(person):
    rows = []
    for ntype in ntypes.all_types():
        if ntype.audience == ntypes.AUDIENCE_BOARD:
            continue
        if not slots.module_on(ntype.module, person.association):
            continue
        chosen = notify_services._preferred(ntype, person)
        rows.append(
            {
                "type": ntype,
                "channels": [(key, label, key in chosen) for key, label in PREF_CHANNELS],
            }
        )
    return rows


def _page(request, person, *, form=None, household_form=None, status=200):
    today = today_for(request.association)
    units = _owned_units(person, today)
    resp = _render(
        request,
        "more",
        "portal/profile.html",
        person=person,
        form=form or ContactForm(instance=person),
        household_form=household_form or HouseholdForm(),
        owned_units=units,
        household=_household(units, today),
        consent=reg.current_notice_preference(person),
        pref_rows=_pref_rows(person),
        **consent_context(),
    )
    resp.status_code = status
    return resp


@resident_view
def profile(request):
    person = _require_person(request)
    if request.method == "POST":
        form = ContactForm(request.POST, instance=Person.objects.get(pk=person.pk))
        if form.is_valid():
            reg.update_contact(person, **{f: form.cleaned_data[f] for f in reg.CONTACT_FIELDS})
            messages.success(request, _("Your contact details were saved."))
            return redirect("portal:profile")
        return _page(request, person, form=form, status=400)
    return _page(request, person)


@require_POST
@resident_view
def household_add(request):
    person = _require_person(request)
    today = today_for(request.association)
    units = _owned_units(person, today)
    unit = next((u for u in units if u.number == request.POST.get("unit")), None)
    if unit is None:
        if len(units) != 1:
            raise PermissionDenied
        unit = units[0]
    form = HouseholdForm(request.POST)
    if not form.is_valid():
        return _page(request, person, household_form=form, status=400)
    member = reg.find_or_create_person(
        request.association, form.cleaned_data["full_name"], form.cleaned_data["email"]
    )
    reg.add_occupancy(
        unit, member, today, kind=Occupancy.Kind.RESIDENT, notes=_("Added by an owner")
    )
    messages.success(request, _("Household member added."))
    return redirect("portal:profile")


@require_POST
@resident_view
def household_end(request, pk):
    person = _require_person(request)
    today = today_for(request.association)
    occ = get_object_or_404(
        Occupancy,
        pk=pk,
        kind=Occupancy.Kind.RESIDENT,
        unit__in=_owned_units(person, today),
    )
    reg.end_occupancy(occ, today)
    messages.success(request, _("Household member removed."))
    return redirect("portal:profile")


@require_POST
@resident_view
def consent(request):
    person = _require_person(request)
    action = request.POST.get("action")
    source = (
        NoticePreference.Source.INVITE
        if request.POST.get("context") == "invite"
        else NoticePreference.Source.PORTAL
    )
    target = "portal:home" if source == NoticePreference.Source.INVITE else "portal:profile"
    try:
        if action == "give":
            if request.POST.get("agree") != "yes":
                raise ValidationError(_("Tick the box to agree to electronic delivery."))
            if request.POST.get("email", "").strip().lower() != (person.email or "").lower():
                raise ValidationError(_("Your email address changed. Check it and agree again."))
            if request.POST.get("version") != consent_text.VERSION:
                raise ValidationError(_("The consent text changed. Read it and agree again."))
            reg.record_notice_preference(
                person,
                NoticePreference.Delivery.EMAIL,
                e_delivery_consent=True,
                source=source,
                recorded_by=request.user,
                note="consent given in portal",
                consent_text_version=consent_text.VERSION,
            )
            messages.success(request, _("You'll get official notices by email."))
        elif action in ("withdraw", "keep_mail"):
            reg.record_notice_preference(
                person,
                NoticePreference.Delivery.POSTAL,
                e_delivery_consent=False,
                source=source,
                recorded_by=request.user,
                note="consent withdrawn in portal" if action == "withdraw" else "chose mail",
            )
            messages.success(request, _("You'll get official notices by postal mail."))
        else:
            raise ValidationError(_("Unknown choice."))
    except ValidationError as exc:
        for msg in exc.messages:
            messages.error(request, msg)
    return redirect(target)


@require_POST
@resident_view
def notifications(request):
    person = _require_person(request)
    for row in _pref_rows(person):
        ntype = row["type"]
        if ntype.mandatory:
            continue
        for key, _label, was_on in row["channels"]:
            now_on = request.POST.get(f"{ntype.key}:{key}") == "on"
            if now_on != was_on:
                notify_services.set_preference(person, ntype.key, key, now_on)
    messages.success(request, _("Notification settings saved."))
    return redirect("portal:profile")
