"""Default PaymentProvider: no online payments; the board records payments by hand."""

from django.utils.translation import gettext as _

from ..payments import PaymentProvider, PaymentRequest, PaymentResult, PaymentStatus


class ManualPayments(PaymentProvider):
    online = False

    def collect(self, request: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            status=PaymentStatus.MANUAL,
            message=_("Online payments are off. Record this payment by hand."),
        )
