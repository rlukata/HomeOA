"""Pay tab (POR-02, FIN-06): pay now and autopay for the signed-in owner.

Everything goes through the association's ``PaymentProvider``: the page lists the
owner's authorized bank accounts (``saved_methods``), links to the provider's bank-linking
page (``setup_url``), and starts debits with ``collect``. The default provider is offline,
so the page then says how to pay instead. Money is recorded only when the provider reports
success; the page shows payments still processing (``pending``).

Amounts are for the owner's own current ownership period and never exceed the
balance due (no prepayment). Pay now confirms on a
second page that carries a one-time key, so a double submit never debits twice.
Behind the residents switch (``resident_view``); the board's preview has no Pay action.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa import providers
from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view
from homeoa.access.services import today_for
from homeoa.payments.services import PaymentError, check_amount
from homeoa.plugins import slots

from .views.base import _render
from .views.mine import my_person

ZERO = Decimal("0.00")


@dataclass
class PayContext:
    unit: object
    period: object
    person: object
    balance: Decimal
    due: Decimal
    pending: list
    provider: object
    methods: list = field(default_factory=list)
    setup_url: str = ""
    #: Filled in by the autopay page only.
    enrollment: object = None
    #: Fee the owner pays on top of what is due (0: absorbed by the association).
    fee: Decimal = ZERO
    #: Rule ``payments.free_method_required``: show the free way to pay next to a fee.
    free_method_required: bool = False
    #: Whether the board has turned online payments on for the association.
    online_payments_on: bool = True

    @property
    def online(self) -> bool:
        return self.online_payments_on and bool(getattr(self.provider, "online", False))

    @property
    def chargeable(self) -> list:
        return [m for m in self.methods if m.chargeable]


def _my_units(request, person, today):
    from homeoa.registry.services import units_owned_by

    if person is None:
        return []
    return sorted(units_owned_by(person, today), key=lambda u: u.number)


def pay_context(request, unit_number: str | None = None) -> PayContext | None:
    """The pay context for one of the owner's current units (``None``: owns none)."""
    from homeoa.ledger.services import unit_balance
    from homeoa.registry.services import ownership_period_on

    today = today_for(request.association)
    person = my_person(request)
    units = _my_units(request, person, today)
    if not units:
        return None
    unit = next((u for u in units if u.number == unit_number), units[0])
    period = ownership_period_on(unit, today)
    provider = providers.get_provider("PaymentProvider", request.association)
    balance = unit_balance(
        unit.pk, as_of=today, ownership_period_id=period.pk, association=unit.association
    )
    pending = provider.pending(unit, period)
    due = max(balance - sum((p.amount for p in pending), ZERO), ZERO)
    return PayContext(
        unit=unit,
        period=period,
        person=person,
        balance=balance,
        due=due,
        pending=pending,
        provider=provider,
        methods=provider.saved_methods(unit, person),
        setup_url=provider.setup_url(unit),
        fee=provider.fee_quote(request.association, due) if due > 0 else ZERO,
        free_method_required=_free_method_required(request.association),
        online_payments_on=slots.module_on("online_payments", request.association),
    )


def _free_method_required(association) -> bool:
    from homeoa import rules

    return rules.value(association, "payments.free_method_required") is True


def _units(request):
    today = today_for(request.association)
    return _my_units(request, my_person(request), today)


def _amount(value) -> Decimal:
    try:
        return check_amount(Decimal(str(value).replace("$", "").replace(",", "").strip()))
    except InvalidOperation, ValueError:
        raise PaymentError(_("Enter an amount in dollars, like 396.63.")) from None


def _check_cap(amount: Decimal, ctx) -> None:
    """An owner payment never exceeds the balance due (no prepayment)."""
    if ctx.due <= 0:
        raise PaymentError(_("Nothing is due right now."))
    if amount > ctx.due:
        raise PaymentError(
            _("You can pay at most your balance of %(max)s.") % {"max": f"${ctx.due:,.2f}"}
        )


def _back(ctx) -> str:
    return reverse("portal:pay") + f"?unit={ctx.unit.number}"


@resident_view(perm=perms.BALANCE_OWN)
def pay(request):
    ctx = pay_context(request, request.GET.get("unit"))
    return _render(
        request, "pay", "portal/pay.html", pay=ctx, switch_units=_units(request) if ctx else []
    )


@resident_view(perm=perms.BALANCE_OWN)
def pay_confirm(request):
    """GET: confirm the amount and bank account. POST: start the debit."""
    ctx = pay_context(request, request.POST.get("unit") or request.GET.get("unit"))
    if ctx is None:
        raise Http404
    if not ctx.online:
        messages.info(request, _("Online payments are not set up yet."))
        return redirect(_back(ctx))
    if not ctx.chargeable:
        messages.info(request, _("Link and verify a bank account first."))
        return redirect(ctx.setup_url or _back(ctx))
    data = request.POST if request.method == "POST" else request.GET
    try:
        amount = _amount(data.get("amount") or ctx.due)
        _check_cap(amount, ctx)
    except PaymentError as exc:
        messages.error(request, str(exc))
        return redirect(_back(ctx))
    if request.method == "POST":
        from homeoa.providers.payments import PaymentRequest, PaymentStatus

        key = str(request.POST.get("key", ""))[:64]
        ref = str(request.POST.get("method", ""))
        if not key or ref not in {m.ref for m in ctx.chargeable}:
            messages.error(request, _("Choose a verified bank account."))
            return redirect(_back(ctx))
        result = ctx.provider.collect(
            PaymentRequest(
                association_id=request.association.pk,
                unit_id=ctx.unit.pk,
                amount=amount,
                description=_("Pay now · Unit %(unit)s") % {"unit": ctx.unit.number},
                payer_email=getattr(request.user, "email", "") or "",
                idempotency_key=f"paynow-{request.user.pk}-{key}",
                mandate_ref=ref,
                ownership_period_id=ctx.period.pk,
            )
        )
        if result.status in (PaymentStatus.PENDING, PaymentStatus.SUCCEEDED):
            messages.success(
                request,
                _("Payment of %(amount)s started. Bank payments take a few business days to clear.")
                % {"amount": f"${amount:,.2f}"},
            )
        else:
            messages.error(
                request,
                _("The payment did not go through: %(why)s")
                % {"why": result.message or _("the bank refused it")},
            )
        return redirect(_back(ctx))
    return _render(
        request,
        "pay",
        "portal/pay_confirm.html",
        pay=ctx,
        amount=amount,
        fee=ctx.provider.fee_quote(request.association, amount),
        key=secrets.token_urlsafe(16),
    )
