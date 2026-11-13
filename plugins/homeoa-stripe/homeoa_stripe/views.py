"""Stripe views: the board's return acknowledgement and the owner's bank linking.

Owner pages live under ``/plugins/stripe/bank/`` and render inside the resident app
(``resident_view``: residents switch on, ``balance.own``, current owners only). See
``bank.py`` for the flow. The SetupIntent in progress is kept in the session; a mandate
waiting for microdeposits is stored and verified later from the same page.
"""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.access.services import today_for

from . import bank, services
from .client import fake_allowed
from .mandates import MandateError, authorization_text, revoke_mandate
from .models import StripeIntent, StripeMandate

SESSION_KEY = "homeoa_stripe_setup"


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def ack_return(request, pk):
    """Clear the "ACH payment … failed" to-do once the board has followed up."""
    intent = get_object_or_404(
        StripeIntent.all_associations, pk=pk, association=request.association
    )
    services.acknowledge_return(intent, user=request.user)
    messages.success(request, _("Noted. The returned payment is off the to-do list."))
    return redirect("dashboard:index")


# ------------------------------------------------------------------ owner bank linking


def _owner_and_unit(request, number):
    from homeoa.portal.views.mine import my_person
    from homeoa.registry.services import units_owned_by

    person = my_person(request)
    if person is None:
        raise Http404
    units = sorted(units_owned_by(person, today_for(request.association)), key=lambda u: u.number)
    unit = next((u for u in units if u.number == number), units[0] if units else None)
    if unit is None:
        raise Http404
    return person, unit


def _is_fake() -> bool:
    return getattr(settings, "HOMEOA_STRIPE_CLIENT", "live") == "fake" and fake_allowed()


def _setup(request, unit) -> dict | None:
    data = request.session.get(SESSION_KEY) or {}
    return data if data.get("unit_id") == unit.pk else None


def _client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "") or ""


def _here(unit) -> str:
    return reverse("homeoa_stripe:bank_link") + f"?unit={unit.number}"


@resident_view(perm=perms.BALANCE_OWN)
def bank_link(request):
    """The bank-account page: link (collect → authorize) and verify microdeposits."""
    from homeoa.portal.views.base import _render

    person, unit = _owner_and_unit(request, request.GET.get("unit"))
    setup = _setup(request, unit)
    if setup is None or request.GET.get("restart"):
        try:
            started = bank.start(unit, person)
        except MandateError as exc:
            messages.error(request, str(exc))
            return redirect(reverse("portal:pay") + f"?unit={unit.number}")
        setup = {"unit_id": unit.pk, **started}
        request.session[SESSION_KEY] = setup
    account = bank.collected_account(setup["setup_id"], association=unit.association)
    waiting = StripeMandate.all_associations.filter(
        association=unit.association,
        unit=unit,
        owner=person,
        revoked_at__isnull=True,
        verified_at__isnull=True,
    )
    linked = StripeMandate.all_associations.filter(
        association=unit.association,
        unit=unit,
        owner=person,
        revoked_at__isnull=True,
        verified_at__isnull=False,
    )
    return _render(
        request,
        "pay",
        "homeoa_stripe/bank.html",
        unit=unit,
        setup=setup,
        account=account,
        authorization=authorization_text(unit, account["last4"]) if account else "",
        waiting=waiting,
        linked=linked,
        fake=_is_fake(),
        publishable_key=getattr(settings, "HOMEOA_STRIPE_PUBLISHABLE_KEY", ""),
        billing_name=person.full_name,
        billing_email=person.email or "",
    )


@require_POST
@resident_view(perm=perms.BALANCE_OWN)
def bank_collect(request):
    """Fake mode only: stands in for Stripe.js collecting the account in the browser."""
    _person, unit = _owner_and_unit(request, request.POST.get("unit"))
    setup = _setup(request, unit)
    if setup is None or not _is_fake():
        raise Http404
    mode = request.POST.get("mode", "")
    try:
        bank.client_for(unit.association).collect_bank_account(setup["setup_id"], mode)
    except Exception as exc:
        messages.error(request, _("The bank account could not be linked: %(e)s") % {"e": exc})
        request.session.pop(SESSION_KEY, None)
    return redirect(_here(unit))


@require_POST
@resident_view(perm=perms.BALANCE_OWN)
def bank_confirm(request):
    """The owner accepted the authorization text: confirm online, store the mandate."""
    person, unit = _owner_and_unit(request, request.POST.get("unit"))
    setup = _setup(request, unit)
    if setup is None:
        return redirect(_here(unit))
    if request.POST.get("accept") != "yes":
        messages.error(request, _("Tick the box to accept the bank authorization."))
        return redirect(_here(unit))
    try:
        mandate = bank.confirm(
            unit,
            person,
            setup["setup_id"],
            user=request.user,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
        )
    except MandateError as exc:
        messages.error(request, str(exc))
        return redirect(_here(unit))
    request.session.pop(SESSION_KEY, None)
    if mandate.chargeable:
        messages.success(
            request,
            _("Bank account ···%(last4)s is linked and verified.") % {"last4": mandate.bank_last4},
        )
        return redirect(reverse("portal:pay") + f"?unit={unit.number}")
    messages.info(
        request,
        _(
            "We sent two small deposits to the account ending in %(last4)s. They arrive in "
            "1-2 business days; enter them here to finish."
        )
        % {"last4": mandate.bank_last4},
    )
    return redirect(_here(unit))


def _own_mandate(request, pk):
    from homeoa.portal.views.mine import my_person

    person = my_person(request)
    return get_object_or_404(
        StripeMandate.all_associations,
        pk=pk,
        association=request.association,
        owner=person,
        revoked_at__isnull=True,
    )


@require_POST
@resident_view(perm=perms.BALANCE_OWN)
def bank_verify(request, pk):
    mandate = _own_mandate(request, pk)
    code = request.POST.get("descriptor_code", "").strip()
    amounts = []
    for name in ("amount1", "amount2"):
        raw = request.POST.get(name, "").strip()
        if raw.isdigit():
            amounts.append(int(raw))
    try:
        bank.verify(mandate, amounts=amounts, descriptor_code=code)
    except MandateError as exc:
        messages.error(request, str(exc))
        return redirect(_here(mandate.unit))
    messages.success(
        request,
        _("Bank account ···%(last4)s is verified.") % {"last4": mandate.bank_last4},
    )
    return redirect(reverse("portal:pay") + f"?unit={mandate.unit.number}")


@require_POST
@resident_view(perm=perms.BALANCE_OWN)
def bank_remove(request, pk):
    """Revoke the authorization (stops future debits, including autopay from it)."""
    from homeoa.payments import autopay
    from homeoa.payments.models import AutopayEnrollment

    mandate = _own_mandate(request, pk)
    revoke_mandate(mandate, user=request.user)
    for enrollment in AutopayEnrollment.all_associations.filter(
        association=request.association,
        unit=mandate.unit,
        mandate_ref=str(mandate.pk),
        cancelled_at__isnull=True,
    ):
        autopay.cancel(enrollment, user=request.user)
    messages.success(
        request, _("Bank account ···%(last4)s removed.") % {"last4": mandate.bank_last4}
    )
    return redirect(reverse("portal:pay") + f"?unit={mandate.unit.number}")
