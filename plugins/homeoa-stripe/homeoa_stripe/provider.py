"""``StripeACH``: the ``PaymentProvider`` this plugin registers (fake or test-mode client)."""

from __future__ import annotations

from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.providers.payments import (
    PaymentProvider,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
    PendingPayment,
    SavedMethod,
)

from .client import get_client
from .models import IntentStatus, StripeIntent, StripeMandate


def mandate_label(mandate: StripeMandate) -> str:
    bank = mandate.bank_name or _("Bank account")
    label = _("%(bank)s ···%(last4)s") % {"bank": bank, "last4": mandate.bank_last4}
    if mandate.verified_at is None:
        label = _("%(label)s (waiting for verification)") % {"label": label}
    return label


class StripeACH(PaymentProvider):
    online = True
    name = gettext_lazy("Stripe ACH (test fake)")

    def __init__(self, installation=None, client=None):
        self.installation = installation
        # The fake in tests/dev/E2E; the real test-mode client otherwise (keys from settings).
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = get_client(self.installation)
        return self._client

    def create_payment(self, unit, amount, mandate, **kwargs):
        from .services import create_payment

        return create_payment(unit, amount, mandate, client=self.client, **kwargs)

    def _mandates(self, unit, person):
        return StripeMandate.all_associations.filter(
            association_id=unit.association_id, unit=unit, owner=person, revoked_at__isnull=True
        )

    def saved_methods(self, unit, person) -> list[SavedMethod]:
        if person is None:
            return []
        return [
            SavedMethod(ref=str(m.pk), label=mandate_label(m), chargeable=m.chargeable)
            for m in self._mandates(unit, person)
        ]

    def setup_url(self, unit) -> str:
        return reverse("homeoa_stripe:bank_link") + f"?unit={unit.number}"

    def pending(self, unit, ownership_period) -> list[PendingPayment]:
        qs = StripeIntent.all_associations.filter(
            association_id=unit.association_id,
            unit=unit,
            ownership_period=ownership_period,
            status=IntentStatus.PROCESSING,
        )
        return [
            PendingPayment(reference=i.intent_id, amount=i.amount, created=i.created_at) for i in qs
        ]

    def fee_quote(self, association, amount):
        from . import fake, services
        from .models import FeeMode

        if services.fee_mode(association) != FeeMode.PASS_THROUGH:
            return Decimal("0.00")
        return fake.ach_fee(amount)

    def collect(self, request: PaymentRequest) -> PaymentResult:
        from homeoa.payments.services import PaymentError
        from homeoa.registry.models import OwnershipPeriod, Unit

        unit = Unit.all_associations.get(pk=request.unit_id, association_id=request.association_id)
        mandate = StripeMandate.all_associations.filter(
            association_id=request.association_id, unit=unit, pk=_int(request.mandate_ref)
        ).first()
        period = None
        if request.ownership_period_id:
            period = OwnershipPeriod.all_associations.get(pk=request.ownership_period_id, unit=unit)
        try:
            intent = self.create_payment(
                unit,
                request.amount,
                mandate,
                ownership_period=period,
                idempotency_key=request.idempotency_key,
            )
        except PaymentError as exc:
            return PaymentResult(status=PaymentStatus.FAILED, message=str(exc))
        if intent.status == IntentStatus.FAILED:
            return PaymentResult(
                status=PaymentStatus.FAILED, reference=intent.intent_id, message=intent.message
            )
        return PaymentResult(
            status=PaymentStatus.PENDING, reference=intent.intent_id, fee=intent.fee
        )


def _int(value) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0
